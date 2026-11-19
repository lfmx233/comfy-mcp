from __future__ import annotations

from pathlib import Path

from comfy_mcp.config import Instance, Settings
from comfy_mcp.server import build_server


def make_settings(url: str = "http://127.0.0.1:9") -> Settings:
    return Settings(
        instances=(Instance(name="fake", url=url),),
        workflows_dir=Path("workflows"),
        runs_dir=Path("runs"),
    )


async def test_tool_is_registered() -> None:
    server = build_server(make_settings())
    listed = await server.list_tools()
    assert [tool.name for tool in listed] == ["comfy_status"]


async def test_unknown_instance_returns_structured_error() -> None:
    server = build_server(make_settings())
    result = await server.call_tool("comfy_status", {"instance": "gpu-9000"})
    payload = result.model_dump(mode="json")
    structured = payload.get("structured_content") or payload.get("structuredContent")
    assert structured["code"] == "INVALID_PARAM"
    assert "unknown instance" in structured["message"]


async def test_unreachable_instance_reports_down() -> None:
    server = build_server(make_settings("http://127.0.0.1:9"))
    result = await server.call_tool("comfy_status", {})
    payload = result.model_dump(mode="json")
    structured = payload.get("structured_content") or payload.get("structuredContent")
    assert structured["reachable"] is False
    assert structured["code"] == "INSTANCE_DOWN"
