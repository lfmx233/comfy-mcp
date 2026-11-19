"""Thin async client over the ComfyUI HTTP + websocket API.

Only the endpoints comfy-mcp actually needs, with everything that can fail
turned into a typed exception so the server layer can map it to an error code.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
import websockets


class ComfyUnreachable(RuntimeError):
    """The instance did not answer at all (connection refused, DNS, timeout)."""


class ComfyHTTPError(RuntimeError):
    """The instance answered with a non-2xx status."""

    def __init__(self, status: int, path: str, body: str) -> None:
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"{path} -> HTTP {status}: {body[:300]}")


@dataclass(frozen=True, slots=True)
class Device:
    name: str
    type: str
    index: int | None
    vram_total: int
    vram_free: int

    @property
    def vram_total_gb(self) -> float:
        return round(self.vram_total / 1024**3, 2)

    @property
    def vram_free_gb(self) -> float:
        return round(self.vram_free / 1024**3, 2)


@dataclass(frozen=True, slots=True)
class QueueState:
    running: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()

    @property
    def depth(self) -> int:
        return len(self.running) + len(self.pending)


@dataclass(frozen=True, slots=True)
class Status:
    """Everything `comfy_status` reports, already flattened for the caller."""

    name: str
    url: str
    reachable: bool
    comfyui_version: str | None = None
    python_version: str | None = None
    pytorch_version: str | None = None
    devices: tuple[Device, ...] = ()
    queue: QueueState = field(default_factory=QueueState)
    error: str | None = None


class EventStream:
    """A websocket that yields JSON frames and skips binary previews.

    Kept as an object (not just an async generator) so the caller can open the socket
    *before* submitting a prompt -- ComfyUI only pushes frames to clients that are
    already connected with the same clientId.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[dict[str, Any]]:
        async for message in self._ws:
            if isinstance(message, bytes):
                continue
            try:
                yield json.loads(message)
            except json.JSONDecodeError:
                continue


class ComfyClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> ComfyClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- low level ---------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._client is None:
            raise RuntimeError("ComfyClient used outside of 'async with'")
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ComfyUnreachable(f"{self.base_url}{path}: {exc}") from exc
        if response.status_code >= 400:
            raise ComfyHTTPError(response.status_code, path, response.text)
        if not response.content:
            return None
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            return response.json()
        return response.content

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def post(self, path: str, payload: Any = None) -> Any:
        return await self._request("POST", path, json=payload)

    # -- endpoints ---------------------------------------------------------

    async def system_stats(self) -> dict[str, Any]:
        return await self.get("/system_stats")

    async def queue(self) -> QueueState:
        raw = await self.get("/queue") or {}
        running = tuple(str(item[1]) for item in raw.get("queue_running", []) if len(item) > 1)
        pending = tuple(str(item[1]) for item in raw.get("queue_pending", []) if len(item) > 1)
        return QueueState(running=running, pending=pending)

    async def object_info(self, node_class: str | None = None) -> dict[str, Any]:
        path = "/object_info" if node_class is None else f"/object_info/{node_class}"
        return await self.get(path) or {}

    async def history(self, prompt_id: str | None = None) -> dict[str, Any]:
        path = "/history" if prompt_id is None else f"/history/{prompt_id}"
        return await self.get(path) or {}

    async def view(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        data = await self.get("/view", filename=filename, subfolder=subfolder, type=type_)
        if not isinstance(data, bytes):
            raise ComfyHTTPError(200, "/view", "expected binary body")
        return data

    async def submit(self, prompt: dict[str, Any], client_id: str | None = None) -> str:
        payload = {"prompt": prompt, "client_id": client_id or uuid.uuid4().hex}
        raw = await self.post("/prompt", payload) or {}
        prompt_id = raw.get("prompt_id")
        if not prompt_id:
            raise ComfyHTTPError(200, "/prompt", f"no prompt_id in response: {raw}")
        return str(prompt_id)

    async def interrupt(self) -> None:
        await self.post("/interrupt")

    async def delete_from_queue(self, prompt_ids: list[str]) -> None:
        await self.post("/queue", {"delete": prompt_ids})

    def _ws_url(self, client_id: str) -> str:
        scheme = "wss" if self.base_url.startswith("https://") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}/ws?clientId={client_id}"

    @asynccontextmanager
    async def event_stream(self, client_id: str) -> AsyncIterator[EventStream]:
        """Open the event socket; entering the context means it is connected.

        Opens the socket *before* the prompt is submitted so no frame is missed.
        """
        url = self._ws_url(client_id)
        try:
            async with websockets.connect(url, max_size=None) as ws:
                yield EventStream(ws)
        except (OSError, websockets.WebSocketException) as exc:
            raise ComfyUnreachable(f"{url}: {exc}") from exc

    async def events(self, client_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded websocket messages until the socket closes."""
        async with self.event_stream(client_id) as stream:
            async for event in stream:
                yield event

    # -- aggregate ---------------------------------------------------------

    async def status(self, name: str) -> Status:
        """Best-effort snapshot; never raises for an unreachable instance."""
        try:
            stats = await self.system_stats()
            queue = await self.queue()
        except (ComfyUnreachable, ComfyHTTPError) as exc:
            return Status(name=name, url=self.base_url, reachable=False, error=str(exc))

        system = stats.get("system", {}) or {}
        devices = tuple(
            Device(
                name=str(d.get("name", "unknown")),
                type=str(d.get("type", "unknown")),
                index=d.get("index"),
                vram_total=int(d.get("vram_total") or 0),
                vram_free=int(d.get("vram_free") or 0),
            )
            for d in stats.get("devices", []) or []
        )
        return Status(
            name=name,
            url=self.base_url,
            reachable=True,
            comfyui_version=system.get("comfyui_version"),
            python_version=system.get("python_version"),
            pytorch_version=system.get("pytorch_version"),
            devices=devices,
            queue=queue,
        )
