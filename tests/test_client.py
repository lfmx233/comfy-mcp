from __future__ import annotations

import httpx2
import pytest

from comfy_mcp.client import ComfyClient, ComfyHTTPError, ComfyUnreachable
from tests.fake_comfy.app import PNG_1X1, FakeState, create_app


@pytest.fixture
def state() -> FakeState:
    return FakeState()


@pytest.fixture
async def client(state: FakeState):
    app = create_app(state)
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fake")
    comfy = ComfyClient("http://fake", client=http)
    await comfy.__aenter__()
    try:
        yield comfy
    finally:
        await comfy.__aexit__(None, None, None)
        await http.aclose()


async def test_system_stats(client: ComfyClient) -> None:
    stats = await client.system_stats()
    assert stats["system"]["comfyui_version"] == "0.3.60"
    assert stats["devices"][0]["type"] == "cuda"


async def test_queue_empty(client: ComfyClient) -> None:
    queue = await client.queue()
    assert queue.running == ()
    assert queue.pending == ()
    assert queue.depth == 0


async def test_queue_reflects_state(client: ComfyClient, state: FakeState) -> None:
    state.running.append([0, "pid-running", {}, {}, []])
    state.pending.append([1, "pid-pending", {}, {}, []])
    queue = await client.queue()
    assert queue.running == ("pid-running",)
    assert queue.pending == ("pid-pending",)
    assert queue.depth == 2


async def test_object_info_all_and_one(client: ComfyClient) -> None:
    everything = await client.object_info()
    assert "KSampler" in everything
    one = await client.object_info("KSampler")
    assert set(one) == {"KSampler"}


async def test_history_and_view(client: ComfyClient) -> None:
    prompt_id = await client.submit({})
    entry = await client.history(prompt_id)
    assert entry[prompt_id]["status"]["status_str"] == "success"

    image = await client.view("fake_00001_.png")
    assert image == PNG_1X1

    history = await client.history()
    assert prompt_id in history


async def test_submit_sends_prompt(client: ComfyClient, state: FakeState) -> None:
    prompt_id = await client.submit({"3": {"class_type": "KSampler"}}, client_id="abc")
    assert prompt_id
    assert state.submitted[-1]["client_id"] == "abc"
    assert state.submitted[-1]["prompt"] == {"3": {"class_type": "KSampler"}}


async def test_http_error_raises(client: ComfyClient, state: FakeState) -> None:
    state.fail_prompt = True
    with pytest.raises(ComfyHTTPError) as info:
        await client.submit({})
    assert info.value.status == 400


async def test_unreachable_raises() -> None:
    async with ComfyClient("http://127.0.0.1:9", timeout=1.0) as comfy:
        with pytest.raises(ComfyUnreachable):
            await comfy.system_stats()


async def test_status_ok(client: ComfyClient) -> None:
    status = await client.status("local")
    assert status.reachable is True
    assert status.comfyui_version == "0.3.60"
    assert status.devices[0].vram_total_gb == 8.0
    assert status.error is None


async def test_status_unreachable_is_reported_not_raised() -> None:
    async with ComfyClient("http://127.0.0.1:9", timeout=1.0) as comfy:
        status = await comfy.status("dead")
    assert status.reachable is False
    assert status.error
