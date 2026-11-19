"""Verify D4 against a real ComfyUI: manifests, previews, resources, restart.

Run with the venv's python from the repo root:

    .venv\\Scripts\\python.exe scripts\\verify_d4.py

The restart check is a real child process: it loads the runs/ directory from disk and
answers comfy_job for a job this process created, which is the whole point of D4.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO)]

from comfy_mcp.config import load_settings  # noqa: E402
from comfy_mcp.resources import image_size  # noqa: E402
from comfy_mcp.server import build_server  # noqa: E402

PROMPT = "1girl, solo, silver hair, blue eyes, school uniform, cherry blossoms, best quality"
BUDGET_BYTES = 20_000


def payload_of(result: Any) -> dict[str, Any]:
    dumped = result.model_dump(mode="json")
    payload = dumped.get("structured_content") or dumped.get("structuredContent")
    if not isinstance(payload, dict):
        raise SystemExit(f"tool returned no structured content: {dumped}")
    return payload


def kinds(result: Any) -> list[str]:
    return [block.type for block in result.content]


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'ok' if condition else 'fail'}] {label}{(' — ' + detail) if detail else ''}")
    return condition


async def job_lookup(job_id: str) -> dict[str, Any]:
    """Read one job from a store built only from what is on disk."""
    settings = load_settings(os.environ)
    server = build_server(settings)
    return payload_of(await server.call_tool("comfy_job", {"job_id": job_id}))


async def main(args: argparse.Namespace) -> int:
    if args.restart_check:
        job = await job_lookup(args.restart_check)
        print("RESTART: " + json.dumps(job, ensure_ascii=False))
        return 0

    settings = load_settings(os.environ)
    server = build_server(settings)
    passed = True
    started = time.time()

    status = payload_of(await server.call_tool("comfy_status", {}))
    passed &= check(
        "comfy_status reaches the instance",
        bool(status.get("reachable")),
        f"{status.get('url')} {status.get('comfyui_version')}",
    )

    result = await server.call_tool(
        "comfy_run",
        {
            "workflow": "txt2img_anime",
            "params": {"prompt": PROMPT, "steps": 8, "seed": 1234},
            "timeout_s": 180,
        },
    )
    run = payload_of(result)
    passed &= check(
        "comfy_run succeeded", run.get("status") == "succeeded", f"{run.get('elapsed_s')}s"
    )
    outputs = run.get("outputs") or []
    expected = ["text", "image"] + ["resource_link"] * len(outputs)
    passed &= check("one text block, one preview, one link per output", kinds(result) == expected)

    manifest = run.get("manifest") or {}
    models = manifest.get("models") or []
    passed &= check("manifest carries the workflow hash", bool(manifest.get("workflow_hash")))
    passed &= check(
        "manifest lists the checkpoint",
        any(model.get("name", "").endswith(".safetensors") for model in models),
        str([model.get("name") for model in models]),
    )
    if settings.models_dir is not None:
        digests = [model.get("sha256") for model in models]
        passed &= check(
            "model digests were computed",
            all(isinstance(digest, str) and len(digest) == 64 for digest in digests),
            str([(digest or "")[:12] for digest in digests]),
        )
    else:
        print("[skip] model digests — set COMFY_MCP_MODELS_DIR to enable")

    first = outputs[0] if outputs else {}
    passed &= check(
        "output size was read from the image bytes",
        (first.get("width"), first.get("height")) == (512, 512),
        f"{first.get('width')}x{first.get('height')}",
    )

    prompt_id = run.get("prompt_id")
    if prompt_id and outputs:
        contents = list(await server.read_resource(f"comfy://output/{prompt_id}/0"))
        body = contents[0].content
        passed &= check(
            "comfy://output resource returns the original PNG",
            isinstance(body, bytes)
            and body.startswith(b"\x89PNG")
            and image_size(body) == (512, 512),
            f"{len(body)} bytes, {image_size(body)}",
        )
        preview_block = result.content[1]
        import base64

        preview_bytes = base64.b64decode(preview_block.data)
        passed &= check(
            "the inline preview is smaller than the original",
            len(preview_bytes) < len(body),
            f"{len(preview_bytes)} < {len(body)} bytes",
        )
    else:
        passed &= check("comfy://output resource returns the original PNG", False, "no prompt id")

    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--restart-check", str(run.get("job_id"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ.copy(),
        cwd=REPO,
        timeout=120,
    )
    line = next((ln for ln in child.stdout.splitlines() if ln.startswith("RESTART: ")), "")
    recovered = json.loads(line[len("RESTART: ") :]) if line else {}
    passed &= check(
        "a fresh process still answers comfy_job from runs/",
        recovered.get("status") == "succeeded"
        and (recovered.get("manifest") or {}).get("workflow_hash") == manifest.get("workflow_hash"),
        f"job={recovered.get('job_id')} status={recovered.get('status')}",
    )

    batch = payload_of(
        await server.call_tool(
            "comfy_run",
            {
                "workflow": "txt2img_anime",
                "params": {"prompt": PROMPT, "steps": 6, "seed": 99, "batch_size": 4},
                "timeout_s": 240,
            },
        )
    )
    encoded = len(json.dumps(batch, ensure_ascii=False).encode("utf-8"))
    batch_outputs = batch.get("outputs") or []
    passed &= check(
        f"four outputs fit in the context budget ({encoded} < {BUDGET_BYTES} bytes)",
        encoded < BUDGET_BYTES and len(batch_outputs) == 4,
        f"{len(batch_outputs)} outputs, {encoded} bytes of JSON",
    )

    print(f"elapsed {time.time() - started:.1f}s")
    print("VERIFY_D4:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify D4 against a real ComfyUI")
    parser.add_argument("--restart-check", metavar="JOB_ID", help="child mode: look one job up")
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(build_parser().parse_args())))
