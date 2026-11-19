"""Live acceptance check for D2 against a real ComfyUI.

For every registered workflow: validate the pinned graph against the instance's own
/object_info, render a prompt with the declared defaults, POST it to /prompt, and cancel it
right away. We only care that the server *accepts* the graph -- not that it finishes, so the
8 GB card never has to render an SDXL batch.

    python scripts/verify_d2.py            # validate + submit + cancel
    python scripts/verify_d2.py --dry-run  # validate only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from comfy_mcp.client import ComfyClient  # noqa: E402
from comfy_mcp.config import Instance  # noqa: E402
from comfy_mcp.registry import RegistryError, apply_params, load_registry, resolve_params  # noqa: E402
from comfy_mcp.registry import validate_workflow  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"))
    parser.add_argument("--dry-run", action="store_true", help="validate only, never submit")
    args = parser.parse_args()

    failures = 0
    async with ComfyClient(args.url) as client:
        node_defs = await client.object_info()
        registry = load_registry(ROOT / "workflows", node_defs)
        print(f"{len(node_defs)} node classes from {args.url}, {len(registry)} workflows")
        for spec in registry.list():
            issues = validate_workflow(spec, node_defs)
            status = "ok" if not issues else "FAIL"
            print(f"\n[{status}] {spec.name}  hash={spec.graph_hash[:19]}…")
            for issue in issues:
                print(f"   {issue.code}: {issue.message}")
            if issues:
                failures += 1
                continue
            if args.dry_run:
                continue
            params = {"prompt": "verify_d2 smoke test"} if "prompt" in spec.params else {}
            api = apply_params(spec, resolve_params(spec, params))
            prompt_id = await client.submit(api, client_id=uuid.uuid4().hex)
            print(f"   accepted by /prompt as {prompt_id}")
            await client.delete_from_queue([prompt_id])
            await client.interrupt()
            print("   cancelled before it could burn the GPU")

    print("\nVERIFY_D2:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except RegistryError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
