"""End-to-end smoke: spawn the server over stdio and ask it for comfy_status.

Run the fake ComfyUI first (uvicorn tests.fake_comfy.app:app --port 8899),
then: python scripts/smoke_d1.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

REPO = Path(__file__).resolve().parents[1]


async def main() -> int:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO / "src"),
        "COMFY_MCP_INSTANCES": os.environ.get(
            "COMFY_MCP_INSTANCES", str(REPO / "tests" / "fixtures" / "fake_instances.yaml")
        ),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "comfy_mcp"], env=env, cwd=str(REPO)
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        names = [tool.name for tool in tools.tools]
        print("tools:", names)

        result = await client.call_tool("comfy_status", {})
        payload = result.model_dump(mode="json")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        structured = payload.get("structured_content") or payload.get("structuredContent")
        ok = (
            names == ["comfy_status"]
            and structured
            and structured.get("reachable") is True
            and bool(structured.get("comfyui_version"))
        )
        print("SMOKE:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
