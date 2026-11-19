"""Job table and progress math, without a server in the way."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_mcp.errors import ERRORS, classify_execution_error, error
from comfy_mcp.jobs import JobStore, Progress

FIXTURES = Path(__file__).parent / "fixtures"


def test_progress_is_monotonic_and_capped_before_success() -> None:
    tracker = Progress(["1", "2", "3"])
    seen = [
        tracker.feed({"type": "executing", "data": {"node": "1"}}),
        tracker.feed({"type": "progress", "data": {"node": "1", "value": 1, "max": 2}}),
        tracker.feed({"type": "executed", "data": {"node": "1"}}),
        tracker.feed({"type": "progress", "data": {"node": "2", "value": 1, "max": 4}}),
    ]
    assert seen == sorted(seen)
    assert seen[0] == 0.0
    assert seen[-1] < 50.0
    assert all(value < 100.0 for value in seen)
    assert tracker.finish() == 100.0


def test_progress_never_goes_backwards_on_late_frames() -> None:
    tracker = Progress(["1", "2"])
    tracker.feed({"type": "progress", "data": {"node": "1", "value": 9, "max": 10}})
    high = tracker.percent
    tracker.feed({"type": "progress", "data": {"node": "1", "value": 1, "max": 10}})
    assert tracker.percent == high


def test_progress_counts_cached_nodes_as_done() -> None:
    tracker = Progress(["1", "2", "3", "4"])
    tracker.feed({"type": "execution_cached", "data": {"nodes": ["1", "2"]}})
    assert tracker.percent == pytest.approx(50.0)


def test_progress_finishes_when_executing_has_no_node() -> None:
    tracker = Progress(["1"])
    assert tracker.feed({"type": "executing", "data": {"node": None}}) == 100.0


def test_store_reuses_job_within_window() -> None:
    store = JobStore(idem_window_s=600)
    first = store.create(workflow="smoke", params={"seed": 1}, instance="fake", idempotency_key="k")
    assert store.find_by_idempotency("k") is first
    store.finish(first, "succeeded")
    assert store.find_by_idempotency("k") is first
    assert store.find_by_idempotency("missing") is None


def test_store_expires_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore(idem_window_s=1)
    job = store.create(workflow="smoke", params={}, instance="fake", idempotency_key="k")
    monkeypatch.setattr("comfy_mcp.jobs._now", lambda: job.created_at + 5)
    assert store.find_by_idempotency("k") is None


def test_store_trims_old_finished_jobs() -> None:
    store = JobStore(keep=2)
    first = store.create(workflow="smoke", params={}, instance="fake")
    store.finish(first, "succeeded")
    store.create(workflow="smoke", params={}, instance="fake")
    store.create(workflow="smoke", params={}, instance="fake")
    assert len(store) == 2
    assert store.get(first.job_id) is None


def test_elapsed_is_zero_before_start() -> None:
    job = JobStore().create(workflow="smoke", params={}, instance="fake")
    assert job.elapsed_s == 0.0
    assert job.info()["status"] == "queued"


def test_error_envelope_defaults() -> None:
    payload = error("TIMEOUT", "too slow", job_id="abc")
    assert payload["retryable"] is True
    assert payload["job_id"] == "abc"
    assert payload["hint"]
    assert set(payload) == {"code", "message", "hint", "retryable", "job_id"}


def test_execution_error_classification() -> None:
    assert classify_execution_error("CUDA out of memory. Tried to allocate") == "CUDA_OOM"
    assert classify_execution_error("RuntimeError: boom") == "EXECUTION_FAILED"
    assert classify_execution_error(None) == "EXECUTION_FAILED"


def test_every_code_has_a_hint() -> None:
    assert len(ERRORS) == 10
    for code, (retryable, hint) in ERRORS.items():
        assert isinstance(retryable, bool), code
        assert hint, code


def test_real_frames_from_comfyui_0360_drive_progress() -> None:
    """Frames captured from a real 0.3.60 run (tests/fixtures/ws_frames_real.json)."""
    frames = json.loads(FIXTURES.joinpath("ws_frames_real.json").read_text(encoding="utf-8"))
    node_ids = {str(n) for n in range(1, 20)}
    tracker = Progress(node_ids)
    samples = [tracker.feed(frame) for frame in frames]
    assert samples == sorted(samples), "progress must never go backwards"
    assert samples[-1] == 100.0
    intermediate = {value for value in samples if 0.0 < value < 100.0}
    assert len(intermediate) >= 3, f"expected real intermediate steps, got {sorted(intermediate)}"

    # progress_state is what 0.3.x actually sends; the old progress frames are extra.
    state_only = [f for f in frames if f["type"] != "progress"]
    second = Progress(node_ids)
    state_samples = [second.feed(frame) for frame in state_only]
    assert state_samples[-1] == 100.0
    assert len({v for v in state_samples if 0.0 < v < 100.0}) >= 3
