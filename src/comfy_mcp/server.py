"""The MCP surface: a small, curated set of tools over ComfyUI."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from . import __version__
from .client import ComfyClient
from .config import ConfigError, Instance, Settings, load_settings

INSTRUCTIONS = """ComfyUI, curated.

This server runs workflows that have been registered in the workflows directory
and only the parameters those workflows declare. Prefer comfy_status first to
see what is available and how busy the instance is.
"""


def error(code: str, message: str, *, hint: str = "", retryable: bool = False) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "hint": hint,
        "retryable": retryable,
        "job_id": None,
    }


def _resolve(settings: Settings, instance: str | None) -> Instance | dict[str, Any]:
    try:
        return settings.instance(instance)
    except ConfigError as exc:
        return error(
            "INVALID_PARAM",
            str(exc),
            hint="Call comfy_status without an instance name to list the default.",
        )


def build_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or load_settings()
    server = MCPServer(name="comfy-mcp", version=__version__, instructions=INSTRUCTIONS)

    @server.tool()
    async def comfy_status(instance: str | None = None) -> dict[str, Any]:
        """Report whether a ComfyUI instance is reachable and how busy it is.

        Returns version info, GPU devices with free VRAM, and the current queue
        depth. Call this before submitting work to avoid queueing onto a
        machine that is already saturated.
        """
        resolved = _resolve(settings, instance)
        if isinstance(resolved, dict):
            return resolved

        async with ComfyClient(resolved.http_base) as client:
            status = await client.status(resolved.name)

        return {
            "instance": status.name,
            "url": status.url,
            "reachable": status.reachable,
            "comfyui_version": status.comfyui_version,
            "python_version": status.python_version,
            "pytorch_version": status.pytorch_version,
            "devices": [
                {
                    "name": d.name,
                    "type": d.type,
                    "vram_total_gb": d.vram_total_gb,
                    "vram_free_gb": d.vram_free_gb,
                }
                for d in status.devices
            ],
            "queue": {
                "running": len(status.queue.running),
                "pending": len(status.queue.pending),
            },
            "error": status.error,
            "code": None if status.reachable else "INSTANCE_DOWN",
        }

    return server


def main() -> None:
    settings = load_settings()
    build_server(settings).run(transport=settings.transport)
