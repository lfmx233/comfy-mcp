"""End-to-end demo against a real ComfyUI: three workflows, live progress, reproducibility.

    .venv\\Scripts\\python.exe scripts\\demo.py                 # full demo, ~2 min on a 4060
    .venv\\Scripts\\python.exe scripts\\demo.py --repro JOB_ID  # re-run a recorded manifest

The full demo leaves four things behind in demo/:

    outputs/<workflow>.png   原始输出（未缩放、未重编码）
    contact-sheet.jpg        三张图拼成一张，给 README 用
    report.json              每个 job 的 id / 耗时 / 字节 sha256 / 参数
    transcript.txt           终端里打印过的每一行

--repro 是给「可复现」这句话做证人的：它从 runs/<job_id>.json 里把参数原样读出来，
换个 idempotency key 重新提交，再把新图与 report.json 里记的 sha256 比一次。
想验证冷启动（关掉 ComfyUI 再起，节点缓存清空），就先重启实例再跑 --repro。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO)]

from comfy_mcp.client import ComfyClient  # noqa: E402
from comfy_mcp.config import load_settings  # noqa: E402
from comfy_mcp.jobs import Progress  # noqa: E402
from comfy_mcp.manifest import ManifestStore  # noqa: E402
from comfy_mcp.registry import apply_params, load_registry, resolve_params  # noqa: E402
from comfy_mcp.server import build_server  # noqa: E402

DEMO = REPO / "demo"
OUTPUTS = DEMO / "outputs"
PROMPT = "1girl, solo, silver hair, blue eyes, school uniform, cherry blossoms, best quality"

# (workflow, params) —— 三个工作流各一次，参数都写死在脚本里，方便对照 manifest。
PLAN: list[tuple[str, dict[str, Any]]] = [
    ("txt2img_anime", {"prompt": PROMPT, "steps": 8, "seed": 424242, "batch_size": 1}),
    (
        "img2img",
        {"prompt": PROMPT, "steps": 8, "seed": 777, "denoise": 0.55, "image": "example.png"},
    ),
    (
        "sdxl_txt2img",
        # refiner_seed 默认是 -1（每次随机），钉住它，三次跑出来才是同一张图。
        {
            "prompt": PROMPT,
            "steps": 8,
            "seed": 2026,
            "refiner_seed": 7,
            "width": 768,
            "height": 768,
        },
    ),
]


@dataclass
class Session:
    """Everything the demo learned, so report.json can be written in one place."""

    lines: list[str] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    repro: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)


def payload_of(result: Any) -> dict[str, Any]:
    dumped = result.model_dump(mode="json")
    payload = dumped.get("structured_content") or dumped.get("structuredContent")
    if not isinstance(payload, dict):
        raise SystemExit(f"tool returned no structured content: {dumped}")
    return payload


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def fetch(server: Any, prompt_id: str, index: int) -> bytes:
    contents = list(await server.read_resource(f"comfy://output/{prompt_id}/{index}"))
    body = contents[0].content
    if not isinstance(body, bytes):
        raise SystemExit(f"resource returned {type(body).__name__}, not bytes")
    return body


async def run_workflow(
    session: Session, server: Any, workflow: str, params: dict[str, Any], timeout_s: float
) -> dict[str, Any] | None:
    started = time.perf_counter()
    payload = payload_of(
        await server.call_tool(
            "comfy_run", {"workflow": workflow, "params": params, "timeout_s": timeout_s}
        )
    )
    wall = time.perf_counter() - started
    status = payload.get("status")
    if status != "succeeded":
        error = payload.get("error") or {}
        session.say(f"  [fail] {workflow}: {status} {error.get('code')} {error.get('message')}")
        session.runs.append({"workflow": workflow, "status": status, "error": error})
        return None

    prompt_id = payload.get("prompt_id") or ""
    outputs = payload.get("outputs") or []
    head = outputs[0] if outputs else {}
    body = await fetch(server, prompt_id, 0)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    (OUTPUTS / f"{workflow}.png").write_bytes(body)

    manifest = payload.get("manifest") or {}
    record = {
        "workflow": workflow,
        "status": status,
        "job_id": payload.get("job_id"),
        "prompt_id": prompt_id,
        "elapsed_s": round(float(payload.get("elapsed_s") or 0.0), 2),
        "wall_s": round(wall, 2),
        "outputs": len(outputs),
        "width": head.get("width"),
        "height": head.get("height"),
        "bytes": len(body),
        "sha256": digest(body),
        "workflow_hash": manifest.get("workflow_hash"),
        "params": manifest.get("params"),
        "models": [model.get("name") for model in manifest.get("models") or []],
    }
    session.runs.append(record)
    size = f"{record['width']}x{record['height']}" if record["width"] else "?"
    session.say(
        f"  [ok] {workflow}: {record['job_id']} {record['elapsed_s']}s  {size}  "
        f"{len(body) / 1024:.0f} KB  sha256:{record['sha256'][:12]}"
    )
    return record


async def trace_progress(session: Session, settings: Any) -> None:
    """Watch the websocket directly, so the numbers are not the server's own claim."""
    instance = settings.instance(None)
    async with ComfyClient(instance.http_base, timeout=30.0) as client:
        registry = load_registry(settings.workflows_dir, await client.object_info())
        spec = registry.get("txt2img_anime")
        params = {"prompt": PROMPT, "steps": 10, "seed": 424242}
        api = apply_params(spec, resolve_params(spec, params))
        tracker = Progress(api.keys())
        client_id = f"comfy-mcp-demo-{int(time.time())}"
        marks: list[tuple[float, float]] = []
        kinds: dict[str, int] = {}
        started = 0.0
        async with client.event_stream(client_id) as stream:
            prompt_id = await client.submit(api, client_id=client_id)
            started = time.perf_counter()
            async for event in stream:
                kind = str(event.get("type"))
                kinds[kind] = kinds.get(kind, 0) + 1
                before = tracker.percent
                tracker.feed(event)
                if tracker.percent != before:
                    marks.append((time.perf_counter() - started, round(tracker.percent, 1)))
                if kind in {"execution_success", "execution_error", "execution_interrupted"}:
                    break
                if kind == "executing" and event.get("data", {}).get("node") is None:
                    break
        tracker.finish()
        total = time.perf_counter() - started

    rising = all(b[1] > a[1] for a, b in zip(marks, marks[1:], strict=False))
    session.progress = {
        "prompt_id": prompt_id,
        "frames": sum(kinds.values()),
        "kinds": kinds,
        "total_s": round(total, 2),
        "marks": [[round(s, 2), pct] for s, pct in marks],
        "final": tracker.percent,
    }
    session.say(f"  [ok] ws: {sum(kinds.values())} 帧 / {len(marks)} 次进度变化，共 {total:.1f}s")
    for seconds, percent in marks:
        # 用「时间占比 vs 进度占比」两列直接看它们是不是同一条曲线。
        share = seconds / total * 100 if total else 0.0
        session.say(f"       t={seconds:5.2f}s  进度={percent:5.1f}%  占时间 {share:4.0f}%")
    verdict = "ok" if rising and tracker.percent == 100.0 else "fail"
    session.say(f"  [{verdict}] 进度单调且收在 100%")
    if not rising:
        session.lines.append("PROGRESS-NOT-MONOTONIC")


async def make_contact_sheet(session: Session, names: list[str]) -> Path | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow is a dev dependency
        session.say("  [skip] contact sheet — Pillow is not installed")
        return None

    panels = [OUTPUTS / f"{name}.png" for name in names]
    panels = [path for path in panels if path.exists()]
    if not panels:
        return None
    height = 512
    tiles = []
    sizes = []  # 原图尺寸：面板被统一缩放到 height，标签必须报原始尺寸，否则 768 的图会自称 512
    for path in panels:
        with Image.open(path) as image:
            ratio = height / image.height
            sizes.append((image.width, image.height))
            tiles.append(image.convert("RGB").resize((max(1, int(image.width * ratio)), height)))
    label = 26
    width = sum(tile.width for tile in tiles) + 12 * (len(tiles) + 1)
    sheet = Image.new("RGB", (width, height + label + 12), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)
    x = 12
    for tile, name, (sw, sh) in zip(tiles, [p.stem for p in panels], sizes, strict=False):
        sheet.paste(tile, (x, 12))
        draw.text((x + 4, height + 14), f"{name}  {sw}x{sh}", fill=(230, 230, 230))
        x += tile.width + 12
    DEMO.mkdir(parents=True, exist_ok=True)
    target = DEMO / "contact-sheet.jpg"
    sheet.save(target, quality=88)
    session.say(f"  [ok] contact sheet: {target.relative_to(REPO)} ({width}x{height + label + 12})")
    return target


async def check_repro(session: Session, server: Any, first: dict[str, Any]) -> None:
    """Resubmit the first run's params under a fresh idempotency key and compare bytes."""
    params = next(params for name, params in PLAN if name == first["workflow"])
    wall_started = time.perf_counter()
    payload = payload_of(
        await server.call_tool(
            "comfy_run",
            {
                "workflow": first["workflow"],
                "params": params,
                "timeout_s": 300,
                "idempotency_key": f"demo-repro-{int(time.time())}",
            },
        )
    )
    wall = time.perf_counter() - wall_started
    prompt_id = payload.get("prompt_id") or ""
    body = await fetch(server, prompt_id, 0)
    again = digest(body)
    same = again == first["sha256"]
    session.repro = {
        "job_id": payload.get("job_id"),
        "sha256": again,
        "same_as": first["job_id"],
        "identical": same,
        "wall_s": round(wall, 2),
        "note": "served from ComfyUI's node cache" if wall < 1.0 else "recomputed",
    }
    verdict = "ok" if same else "fail"
    session.say(
        f"  [{verdict}] {payload.get('job_id')} 用同一组参数重跑，{wall:.1f}s，"
        f"sha256:{again[:12]} vs {first['sha256'][:12]} → {'完全一致' if same else '不一致'}"
    )
    if same and wall < 1.0:
        session.say("       （这么快是因为 ComfyUI 节点级缓存命中；冷启动验证见 --repro）")


async def run_demo(session: Session, plan: list[tuple[str, dict[str, Any]]]) -> int:
    settings = load_settings(os.environ)
    server = build_server(settings)
    t0 = time.time()

    session.say("== 1. 实例 ==")
    status = payload_of(await server.call_tool("comfy_status", {}))
    if not status.get("reachable"):
        session.say(f"  [fail] {status.get('url')} unreachable: {status.get('error')}")
        return 1
    device = (status.get("devices") or [{}])[0]
    session.meta = {
        "url": status.get("url"),
        "comfyui_version": status.get("comfyui_version"),
        "python": status.get("python_version"),
        "pytorch": status.get("pytorch_version"),
        "device": device.get("name"),
        "vram_gb": device.get("vram_total_gb"),
        "queue": status.get("queue"),
    }
    session.say(
        f"  [ok] {status.get('url')}  ComfyUI {status.get('comfyui_version')}  "
        f"{device.get('name')}  {device.get('vram_free_gb')}/{device.get('vram_total_gb')} GB free"
    )

    listed = payload_of(await server.call_tool("comfy_list_workflows", {}))
    names = [item["name"] for item in listed.get("workflows") or []]
    session.say(f"  [ok] 注册表 {listed.get('count')} 个工作流: {', '.join(names)}")

    session.say("")
    session.say(f"== 2. {len(plan)} 个工作流各跑一次 ==")
    for workflow, params in plan:
        validated = payload_of(
            await server.call_tool("comfy_validate", {"workflow": workflow, "params": params})
        )
        if validated.get("ok") is not True:
            session.say(f"  [fail] {workflow} 预检未过: {validated.get('param_errors')}")
            return 1
        await run_workflow(session, server, workflow, params, timeout_s=420)

    session.say("")
    session.say("== 3. 进度是真实执行映射过来的 ==")
    await trace_progress(session, settings)

    session.say("")
    session.say("== 4. 同一份 manifest 再跑一次，字节是否一致 ==")
    first = next((run for run in session.runs if run["workflow"] == "txt2img_anime"), None)
    if first is None:
        session.say("  [skip] 这一次没跑 txt2img_anime，跳过复现检查")
    else:
        await check_repro(session, server, first)

    session.say("")
    session.say("== 5. 产物 ==")
    await make_contact_sheet(session, [name for name, _ in plan])
    DEMO.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "took_s": round(time.time() - t0, 1),
        "instance": session.meta,
        "runs": session.runs,
        "progress": session.progress,
        "repro": session.repro,
    }
    (DEMO / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if session.progress:
        session.say(
            f"       demo/report.json  demo/contact-sheet.jpg  demo/outputs/*.png "
            f"（{len(session.runs)} 张图）"
        )
    session.say("")
    session.say(f"总耗时 {report['took_s']}s")
    done = all(run.get("status") == "succeeded" for run in session.runs)
    passed = len(session.runs) == len(plan) and done and session.repro.get("identical", True)
    session.say("DEMO: " + ("PASS" if passed else "FAIL"))
    return 0


async def run_repro(session: Session, job_id: str, expected: str | None) -> int:
    """Re-run the manifest recorded for job_id and compare image bytes."""
    settings = load_settings(os.environ)
    store = ManifestStore(settings.runs_dir)
    job = store.read(job_id)
    if job is None:
        session.say(f"  [fail] runs/{job_id}.json 不存在")
        return 1
    if expected is None:
        report = DEMO / "report.json"
        if report.exists():
            data = json.loads(report.read_text(encoding="utf-8"))
            hit = next((run for run in data.get("runs") or [] if run.get("job_id") == job_id), None)
            expected = (hit or {}).get("sha256")
    if not expected:
        session.say("  [fail] 没有可比对的 sha256：--expect 或先跑一次完整 demo")
        return 1

    server = build_server(settings)
    session.say(f"== 用 runs/{job_id}.json 的参数重跑 ==")
    session.say(f"  workflow={job.workflow}  params={json.dumps(job.params, ensure_ascii=False)}")
    payload = payload_of(
        await server.call_tool(
            "comfy_run",
            {
                "workflow": job.workflow,
                "params": job.params,
                "timeout_s": 420,
                "idempotency_key": f"repro-{int(time.time())}",
            },
        )
    )
    prompt_id = payload.get("prompt_id") or ""
    if payload.get("status") != "succeeded":
        session.say(f"  [fail] {payload.get('status')} {(payload.get('error') or {}).get('code')}")
        return 1
    body = await fetch(server, prompt_id, 0)
    got = digest(body)
    same = got == expected
    session.say(f"  期望 {expected[:16]}…  实际 {got[:16]}…  → {'一致' if same else '不一致'}")
    session.say(f"  耗时 {payload.get('elapsed_s')}s，输出 {len(body)} 字节")
    session.say("REPRO: " + ("IDENTICAL" if same else "DIFFERENT"))
    return 0 if same else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="comfy-mcp end-to-end demo")
    parser.add_argument("--repro", metavar="JOB_ID", help="re-run one recorded manifest")
    parser.add_argument("--expect", metavar="SHA256", help="with --repro: expected digest")
    parser.add_argument(
        "--only",
        action="append",
        metavar="WORKFLOW",
        help="run just these workflows (repeatable), e.g. --only txt2img_anime",
    )
    args = parser.parse_args()
    plan = [item for item in PLAN if not args.only or item[0] in args.only]
    unknown = sorted(set(args.only or []) - {name for name, _ in PLAN})
    if unknown:
        parser.error(f"unknown workflow(s): {', '.join(unknown)}")
    session = Session()
    try:
        code = (
            await run_repro(session, args.repro, args.expect)
            if args.repro
            else await run_demo(session, plan)
        )
    finally:
        if session.lines:
            DEMO.mkdir(parents=True, exist_ok=True)
            (DEMO / "transcript.txt").write_text("\n".join(session.lines) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
