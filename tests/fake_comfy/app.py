"""A stand-in ComfyUI server for tests.

Implements the subset of the HTTP + websocket API that comfy-mcp talks to,
with just enough state to make a run look real (prompt ids, queue depth,
history entries, and a websocket that reports progress).
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

DEFAULT_STATS = {
    "system": {
        "os": "nt",
        "comfyui_version": "0.3.60",
        "python_version": "3.12.10 (main, Sep 30 2026, 10:00:00) [MSC v.1944 64 bit (AMD64)]",
        "pytorch_version": "2.8.0+cu129",
        "embedded_python": True,
        "argv": ["main.py", "--listen", "127.0.0.1", "--port", "8188"],
    },
    "devices": [
        {
            "name": "cuda:0 NVIDIA GeForce RTX 4060 Laptop GPU : cudaMallocAsync",
            "type": "cuda",
            "index": 0,
            "vram_total": 8585216000,
            "vram_free": 7451181056,
            "torch_vram_total": 0,
            "torch_vram_free": 0,
        }
    ],
}

OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["sd15.safetensors"], {}]}},
        "output": ["MODEL", "CLIP", "VAE"],
        "name": "CheckpointLoaderSimple",
    },
    "KSampler": {
        "input": {
            "required": {
                "seed": ["INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}],
                "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
            }
        },
        "output": ["LATENT"],
        "name": "KSampler",
    },
    "SaveImage": {
        "input": {
            "required": {
                "images": ["IMAGE", {}],
                "filename_prefix": ["STRING", {"default": "ComfyUI"}],
            }
        },
        "output": [],
        "name": "SaveImage",
        "output_node": True,
    },
}


@dataclass
class FakeState:
    running: list[list[Any]] = field(default_factory=list)
    pending: list[list[Any]] = field(default_factory=list)
    history: dict[str, Any] = field(default_factory=dict)
    interrupts: int = 0
    submitted: list[dict[str, Any]] = field(default_factory=list)
    fail_prompt: bool = False
    stats: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_STATS)))
    clients: dict[str, WebSocket] = field(default_factory=dict)
    # execution simulation
    auto_execute: bool = True
    node_delay: float = 0.01
    progress_steps: int = 3
    failure: str | None = None  # None | "oom" | "boom"
    image_count: int = 2
    interrupt_requested: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def make_error_entry(self, prompt_id: str, message: str) -> dict[str, Any]:
        entry = self.make_entry(prompt_id)
        entry["status"] = {
            "status_str": "error",
            "completed": False,
            "messages": [
                ["execution_start", {"prompt_id": prompt_id}],
                [
                    "execution_error",
                    {
                        "prompt_id": prompt_id,
                        "node_id": "2",
                        "node_type": "KSampler",
                        "exception_message": message,
                    },
                ],
            ],
        }
        return entry

    def make_canceled_entry(self, prompt_id: str) -> dict[str, Any]:
        entry = self.make_entry(prompt_id)
        entry["status"] = {
            "status_str": "error",
            "completed": False,
            "messages": [["execution_interrupted", {"prompt_id": prompt_id}]],
        }
        return entry

    def make_entry(self, prompt_id: str, prompt: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "prompt": [0, prompt_id, prompt or {}, {}, []],
            "outputs": {
                "9": {
                    "images": [
                        {"filename": f"fake_{i:05d}_.png", "subfolder": "", "type": "output"}
                        for i in range(1, self.image_count + 1)
                    ]
                }
            },
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [
                    ["execution_start", {"prompt_id": prompt_id}],
                    ["execution_success", {"prompt_id": prompt_id}],
                ],
            },
        }


def create_app(state: FakeState | None = None) -> Starlette:
    state = state or FakeState()

    async def system_stats(request: Request) -> Response:
        return JSONResponse(state.stats)

    async def queue_endpoint(request: Request) -> Response:
        if request.method == "POST":
            body = await request.json()
            for prompt_id in body.get("delete", []):
                state.pending = [item for item in state.pending if item[1] != prompt_id]
            return JSONResponse({})
        return JSONResponse({"queue_running": state.running, "queue_pending": state.pending})

    async def object_info(request: Request) -> Response:
        node = request.path_params.get("node_class")
        if node is None:
            return JSONResponse(OBJECT_INFO)
        if node not in OBJECT_INFO:
            return JSONResponse({"error": "node not found"}, status_code=404)
        return JSONResponse({node: OBJECT_INFO[node]})

    async def history(request: Request) -> Response:
        prompt_id = request.path_params.get("prompt_id")
        if prompt_id is None:
            return JSONResponse(state.history)
        entry = state.history.get(prompt_id)
        return JSONResponse({prompt_id: entry} if entry else {})

    async def view(request: Request) -> Response:
        filename = request.query_params.get("filename", "")
        if not filename:
            return Response("missing filename", status_code=400)
        return Response(PNG_1X1, media_type="image/png")

    async def send(client_id: str, frame: dict[str, Any]) -> None:
        state.events.append(frame)
        ws = state.clients.get(client_id)
        if ws is None:
            return
        try:
            await ws.send_text(json.dumps(frame))
        except Exception:  # noqa: BLE001 - a dead socket must not kill the executor
            state.clients.pop(client_id, None)

    async def execute(prompt_id: str, prompt: dict[str, Any], client_id: str) -> None:
        nodes = list(prompt.keys())
        await send(client_id, {"type": "execution_start", "data": {"prompt_id": prompt_id}})
        for node in nodes:
            if state.interrupt_requested:
                state.history[prompt_id] = state.make_canceled_entry(prompt_id)
                await send(
                    client_id,
                    {"type": "execution_interrupted", "data": {"prompt_id": prompt_id}},
                )
                return
            await send(
                client_id,
                {"type": "executing", "data": {"node": node, "prompt_id": prompt_id}},
            )
            for value in range(1, state.progress_steps + 1):
                await asyncio.sleep(state.node_delay)
                if state.failure == "oom":
                    message = "CUDA out of memory. Tried to allocate 2.00 GiB"
                elif state.failure == "boom":
                    message = "RuntimeError: mat1 and mat2 shapes cannot be multiplied"
                else:
                    message = None
                if message is not None:
                    state.history[prompt_id] = state.make_error_entry(prompt_id, message)
                    await send(
                        client_id,
                        {
                            "type": "execution_error",
                            "data": {
                                "prompt_id": prompt_id,
                                "node_id": node,
                                "node_type": "KSampler",
                                "exception_message": message,
                            },
                        },
                    )
                    return
                await send(
                    client_id,
                    {
                        "type": "progress",
                        "data": {
                            "value": value,
                            "max": state.progress_steps,
                            "node": node,
                            "prompt_id": prompt_id,
                        },
                    },
                )
            await send(
                client_id,
                {"type": "executed", "data": {"node": node, "prompt_id": prompt_id}},
            )
        await send(client_id, {"type": "execution_success", "data": {"prompt_id": prompt_id}})

    async def prompt(request: Request) -> Response:
        if state.fail_prompt:
            return JSONResponse(
                {"error": {"type": "prompt_outputs_failed_validation"}}, status_code=400
            )
        body = await request.json()
        prompt_id = uuid.uuid4().hex
        state.submitted.append(body)
        state.history[prompt_id] = state.make_entry(prompt_id, body.get("prompt"))
        if state.auto_execute:
            client_id = str(body.get("client_id") or "")
            task = asyncio.create_task(execute(prompt_id, body.get("prompt") or {}, client_id))
            state.tasks.append(task)
        return JSONResponse(
            {"prompt_id": prompt_id, "number": len(state.history), "node_errors": {}}
        )

    async def interrupt(request: Request) -> Response:
        state.interrupts += 1
        state.interrupt_requested = True
        return JSONResponse({})

    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        client_id = websocket.query_params.get("clientId", "")
        state.clients[client_id] = websocket
        try:
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.pop(client_id, None)

    return Starlette(
        routes=[
            Route("/system_stats", system_stats),
            Route("/queue", queue_endpoint, methods=["GET", "POST"]),
            Route("/object_info", object_info),
            Route("/object_info/{node_class}", object_info),
            Route("/history", history),
            Route("/history/{prompt_id}", history),
            Route("/view", view),
            Route("/prompt", prompt, methods=["POST"]),
            Route("/interrupt", interrupt, methods=["POST"]),
            WebSocketRoute("/ws", ws_endpoint),
        ]
    )


app = create_app()
