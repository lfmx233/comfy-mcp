"""Entry point: `comfy-mcp` or `python -m comfy_mcp`.

With no arguments it speaks MCP over stdio. `--check` is a convenience for
humans: it prints the same snapshot `comfy_status` returns, then exits.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .client import ComfyClient
from .config import ConfigError, Settings, load_settings
from .server import build_server


def _format_status(status) -> str:  # noqa: ANN001 - Status, kept local for --check only
    if not status.reachable:
        return f"{status.name:<12} {status.url:<28} UNREACHABLE  {status.error}"
    devices = ", ".join(
        f"{d.type} {d.vram_free_gb}/{d.vram_total_gb}GB free" for d in status.devices
    )
    return (
        f"{status.name:<12} {status.url:<28} {status.comfyui_version:<8} "
        f"queue {len(status.queue.running)}+{len(status.queue.pending):<3} {devices}"
    )


async def _check(settings: Settings) -> int:
    failures = 0
    for instance in settings.instances:
        async with ComfyClient(instance.http_base, timeout=5.0) as client:
            status = await client.status(instance.name)
        print(_format_status(status))
        failures += 0 if status.reachable else 1
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comfy-mcp",
        description="A curated, reproducible MCP server for ComfyUI.",
    )
    parser.add_argument("--version", action="version", version=f"comfy-mcp {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print instance status for every configured ComfyUI and exit",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="override COMFY_MCP_TRANSPORT",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"comfy-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if args.check:
        raise SystemExit(asyncio.run(_check(settings)))

    transport = args.transport or settings.transport
    if transport == "http":
        transport = "streamable-http"
    build_server(settings).run(transport=transport)
