"""Verify D3 against the real ComfyUI on this machine.

Two layers:
  1. the MCP tools themselves (status / list / validate / run / idempotency)
  2. the ws progress mapping, fed with frames a real ComfyUI actually emits

Run with:  $env:PYTHONPATH="src"; python scripts/verify_d3.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from comfy_mcp.client import ComfyClient
from comfy_mcp.config import load_settings
from comfy_mcp.jobs import Progress
from comfy_mcp.registry import apply_params, load_registry, resolve_params
from comfy_mcp.server import build_server

PARAMS: dict[str, Any] = {
    "prompt": "a cat sitting on a windowsill, soft light",
    "negative": "blurry, lowres",
    "steps": 4,
    "width": 512,
    "height": 512,
    "batch_size": 1,
    "seed": 12345,
}


def structured(result: Any) -> dict[str, Any]:
    dump = result.model_dump(mode="json")
    return dump.get("structured_content") or dump.get("structuredContent") or {}


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return ok


async def main() -> int:
    settings = load_settings()
    instance = settings.instance()
    server = build_server(settings)
    ok = True

    async def call(name: str, **kwargs: Any) -> dict[str, Any]:
        return structured(await server.call_tool(name, kwargs))

    status = await call("comfy_status")
    version = str(status.get("comfyui_version"))
    ok &= check(
        "comfy_status",
        status.get("reachable") is True and version.startswith("0.3"),
        f"{version} queue {status.get('queue')} workflows {status.get('workflows')}",
    )

    listed = await call("comfy_list_workflows")
    names = [w["name"] for w in listed.get("workflows", [])]
    ok &= check("comfy_list_workflows", listed.get("count") == 3, ", ".join(names))

    validated = await call("comfy_validate", workflow="txt2img_anime", params=PARAMS)
    ok &= check(
        "comfy_validate",
        validated.get("ok") is True,
        f"ok={validated.get('ok')} issues={validated.get('param_errors')}",
    )

    started = time.perf_counter()
    first = await call("comfy_run", workflow="txt2img_anime", params=PARAMS, timeout_s=180)
    elapsed = time.perf_counter() - started
    outputs = first.get("outputs") or []
    manifest = first.get("manifest") or {}
    ok &= check(
        "comfy_run",
        first.get("status") == "succeeded" and len(outputs) >= 1,
        f"{first.get('job_id')} in {elapsed:.1f}s, {len(outputs)} output(s), "
        f"sha256 missing: {[m['name'] for m in manifest.get('models', []) if not m['sha256']]}",
    )
    if outputs:
        head = outputs[0]
        size = f"{head['width']}x{head['height']}" if head.get("width") else "size pending D4"
        print(f"       first output: {head['resource']} {size} kind={head['kind']}")

    job = await call("comfy_job", job_id=first.get("job_id"))
    ok &= check(
        "comfy_job",
        job.get("status") == "succeeded" and job.get("progress") == 100.0,
        f"status={job.get('status')} progress={job.get('progress')}",
    )

    again = await call("comfy_run", workflow="txt2img_anime", params=PARAMS, timeout_s=180)
    same_job = again.get("job_id") == first.get("job_id")
    ok &= check(
        "idempotency",
        again.get("reused") is True and same_job,
        f"reused={again.get('reused')} same_job={same_job}",
    )

    if "--timeout-check" in sys.argv:
        slow = dict(PARAMS, steps=40, width=1024, height=1024, seed=777)
        timed_out = await call("comfy_run", workflow="txt2img_anime", params=slow, timeout_s=2)
        timed_out_code = (timed_out.get("error") or {}).get("code")
        ok &= check(
            "timeout path",
            timed_out.get("status") == "timeout"
            and timed_out_code == "TIMEOUT"
            and bool(timed_out.get("job_id")),
            f"status={timed_out.get('status')} code={timed_out_code} job={timed_out.get('job_id')}",
        )
        await asyncio.sleep(1.5)
        drained = await call("comfy_status")
        queue = drained.get("queue") or {}
        ok &= check(
            "interrupt drained the queue",
            queue.get("running") == 0 and queue.get("pending") == 0,
            str(queue),
        )

    # Layer 2: feed Progress with frames a real ComfyUI emits.
    async with ComfyClient(instance.http_base, timeout=30.0) as client:
        registry = load_registry(settings.workflows_dir, await client.object_info())
        spec = registry.get("txt2img_anime")
        api = apply_params(spec, resolve_params(spec, PARAMS))
        tracker = Progress(api.keys())
        client_id = "comfy-mcp-verify-d3"
        prompt_id = ""
        async with client.event_stream(client_id) as stream:
            prompt_id = await client.submit(api, client_id=client_id)
            frames = 0
            samples: list[float] = []
            kinds: dict[str, int] = {}
            async for event in stream:
                frames += 1
                kind = str(event.get("type"))
                kinds[kind] = kinds.get(kind, 0) + 1
                tracker.feed(event)
                samples.append(round(tracker.percent, 1))
                if kind in {"execution_success", "execution_error", "execution_interrupted"}:
                    break
                if kind == "executing" and event.get("data", {}).get("node") is None:
                    break
        tracker.finish()
        print(f"       frame kinds: {kinds}")
        rising = all(b >= a for a, b in zip(samples, samples[1:], strict=False))
        print(f"       accepted as {prompt_id}, {frames} ws frame(s), samples: {samples}")
        ok &= check(
            "ws progress",
            tracker.percent == 100.0 and frames >= 3 and rising,
            f"{frames} frames, monotonic={rising}, final={tracker.percent}%",
        )

    print("VERIFY_D3:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
