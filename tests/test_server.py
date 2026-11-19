"""End-to-end tool tests against a live fake ComfyUI (HTTP + websocket)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from comfy_mcp.config import Instance, Settings
from comfy_mcp.server import build_server
from tests.fake_comfy.app import FakeState, create_app

REGISTRY = Path("tests/fixtures/registry")
BROKEN = Path("tests/fixtures/registry_broken")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeUvicorn:
    """Run the fake ComfyUI in its own thread and loop, so client calls see a real socket."""

    def __init__(self, state: FakeState) -> None:
        self.port = _free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(state),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            )
        )
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("fake ComfyUI did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def fake() -> Any:
    state = FakeState()
    server = FakeUvicorn(state)
    server.start()
    try:
        yield state, f"http://127.0.0.1:{server.port}"
    finally:
        server.stop()


def make_settings(url: str, workflows_dir: Path = REGISTRY, max_queue: int = 2) -> Settings:
    return Settings(
        instances=(Instance(name="fake", url=url),),
        workflows_dir=workflows_dir,
        runs_dir=Path("tests/runs"),
        default_timeout_s=5,
        max_queue=max_queue,
    )


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    payload = result.model_dump(mode="json")
    structured = payload.get("structured_content") or payload.get("structuredContent")
    assert isinstance(structured, dict), payload
    return structured


async def test_six_tools_are_registered() -> None:
    server = build_server(make_settings("http://127.0.0.1:9"))
    listed = await server.list_tools()
    assert [tool.name for tool in listed] == [
        "comfy_status",
        "comfy_list_workflows",
        "comfy_validate",
        "comfy_run",
        "comfy_job",
        "comfy_cancel",
    ]


async def test_unknown_instance_returns_structured_error() -> None:
    server = build_server(make_settings("http://127.0.0.1:9"))
    payload = await call(server, "comfy_status", instance="gpu-9000")
    assert payload["code"] == "INVALID_PARAM"
    assert "unknown instance" in payload["message"]
    assert payload["retryable"] is False


async def test_unreachable_instance_reports_down() -> None:
    server = build_server(make_settings("http://127.0.0.1:9"))
    payload = await call(server, "comfy_status")
    assert payload["reachable"] is False
    assert payload["code"] == "INSTANCE_DOWN"


async def test_status_reports_queue_and_workflow_count(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_status")
    assert payload["reachable"] is True
    assert payload["comfyui_version"] == "0.3.60"
    assert payload["queue"] == {"running": 0, "pending": 0}
    assert payload["workflows"] == 1
    assert payload["max_queue"] == 2


async def test_list_workflows_shows_param_schema(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_list_workflows")
    assert payload["count"] == 1
    workflow = payload["workflows"][0]
    assert workflow["name"] == "smoke"
    assert workflow["tags"] == ["test", "fast"]
    assert workflow["params"]["steps"]["maximum"] == 8
    assert workflow["graph_hash"].startswith("sha256:")


async def test_validate_accepts_good_params(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_validate", workflow="smoke", params={"seed": 5})
    assert payload["ok"] is True
    assert payload["missing_nodes"] == []
    assert payload["param_errors"] == []


async def test_validate_reports_bad_param(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_validate", workflow="smoke", params={"steps": 99})
    assert payload["ok"] is False
    assert any("steps" in message for message in payload["param_errors"])


async def test_validate_unknown_workflow(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_validate", workflow="nope")
    assert payload["code"] == "UNKNOWN_WORKFLOW"
    assert "smoke" in payload["message"]


async def test_missing_node_is_caught_at_validate_and_run(fake: Any) -> None:
    # Registry loading is instance-agnostic on purpose: which classes exist is a
    # per-instance fact, so it is checked at validate/run time, not at startup.
    _, url = fake
    server = build_server(make_settings(url, workflows_dir=BROKEN))
    listed = await call(server, "comfy_list_workflows")
    assert listed["count"] == 1

    validated = await call(server, "comfy_validate", workflow="broken")
    assert validated["ok"] is False
    assert any("MissingNode" in message for message in validated["missing_nodes"])

    payload = await call(server, "comfy_run", workflow="broken")
    assert payload["code"] == "NODE_MISSING"
    assert "MissingNode" in payload["message"]


async def test_run_success_returns_manifest_and_outputs(fake: Any) -> None:
    state, url = fake
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_run", workflow="smoke", params={"seed": 7, "steps": 2})
    assert payload["status"] == "succeeded"
    assert payload["elapsed_s"] >= 0
    manifest = payload["manifest"]
    assert manifest["workflow"] == "smoke"
    assert manifest["workflow_hash"].startswith("sha256:")
    assert manifest["params"]["seed"] == 7
    assert manifest["models"] == [{"node": "1", "name": "sd15.safetensors", "sha256": None}]
    assert len(payload["outputs"]) == 2
    assert payload["outputs"][0]["resource"].startswith("comfy://output/")

    progress_frames = [e for e in state.events if e["type"] == "progress"]
    assert len(progress_frames) >= 3
    assert state.events[-1]["type"] == "execution_success"


async def test_run_is_idempotent_for_same_params(fake: Any) -> None:
    state, url = fake
    server = build_server(make_settings(url))
    first = await call(server, "comfy_run", workflow="smoke", params={"seed": 11, "steps": 2})
    second = await call(server, "comfy_run", workflow="smoke", params={"seed": 11, "steps": 2})
    assert second["reused"] is True
    assert second["job_id"] == first["job_id"]
    assert len(state.submitted) == 1


async def test_run_maps_cuda_oom(fake: Any) -> None:
    state, url = fake
    state.failure = "oom"
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_run", workflow="smoke", params={"seed": 3})
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "CUDA_OOM"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["job_id"] == payload["job_id"]


async def test_run_maps_generic_execution_failure(fake: Any) -> None:
    state, url = fake
    state.failure = "boom"
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_run", workflow="smoke", params={"seed": 3})
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "EXECUTION_FAILED"
    assert payload["error"]["retryable"] is False


async def test_run_timeout_keeps_job_id_and_interrupts(fake: Any) -> None:
    state, url = fake
    state.node_delay = 5.0
    server = build_server(make_settings(url))
    payload = await call(server, "comfy_run", workflow="smoke", params={"seed": 3}, timeout_s=0.2)
    assert payload["status"] == "timeout"
    assert payload["error"]["code"] == "TIMEOUT"
    assert payload["error"]["job_id"] == payload["job_id"]
    assert payload["error"]["retryable"] is True
    assert state.interrupts >= 1

    job = await call(server, "comfy_job", job_id=payload["job_id"])
    assert job["status"] == "timeout"


async def test_queue_full_is_retryable(fake: Any) -> None:
    state, url = fake
    state.running = [["1", "p1", {}, {}, []], ["2", "p2", {}, {}, []]]
    server = build_server(make_settings(url, max_queue=2))
    payload = await call(server, "comfy_run", workflow="smoke", params={"seed": 3})
    assert payload["code"] == "QUEUE_FULL"
    assert payload["retryable"] is True


async def test_job_lookup_reports_result(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    run = await call(server, "comfy_run", workflow="smoke", params={"seed": 4})
    job = await call(server, "comfy_job", job_id=run["job_id"])
    assert job["status"] == "succeeded"
    assert job["progress"] == 100.0
    assert len(job["outputs"]) == 2


async def test_job_lookup_unknown_id() -> None:
    server = build_server(make_settings("http://127.0.0.1:9"))
    payload = await call(server, "comfy_job", job_id="deadbeef")
    assert payload["code"] == "INVALID_PARAM"
    assert payload["retryable"] is False


async def test_cancel_finished_job_is_a_noop(fake: Any) -> None:
    _, url = fake
    server = build_server(make_settings(url))
    run = await call(server, "comfy_run", workflow="smoke", params={"seed": 4})
    payload = await call(server, "comfy_cancel", job_id=run["job_id"])
    assert payload["canceled"] is False
    assert payload["queue_removed"] is False
    assert "succeeded" in payload["reason"]
