"""Manifest files: the thing that survives a restart."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from comfy_mcp.jobs import Job
from comfy_mcp.manifest import ManifestError, ManifestStore, from_record, to_record


def make_job(job_id: str = "abc123", **overrides: object) -> Job:
    job = Job(
        job_id=job_id,
        workflow="smoke",
        params={"seed": 1, "steps": 2},
        instance="fake",
        workflow_hash="sha256:abc",
        idempotency_key=f"key-{job_id}",
    )
    job.api = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}}
    }
    job.models = [{"node": "1", "name": "sd15.safetensors", "sha256": "deadbeef"}]
    job.outputs = [{"index": 0, "filename": "a.png", "resource": "comfy://output/p/0"}]
    job.warnings = ["kept"]
    for name, value in overrides.items():
        setattr(job, name, value)
    return job


def test_record_round_trip_keeps_every_field() -> None:
    job = make_job(status="succeeded", progress=100.0, prompt_id="p1")
    restored = from_record(to_record(job))

    assert restored.job_id == job.job_id
    assert restored.workflow == "smoke"
    assert restored.instance == "fake"
    assert restored.params == {"seed": 1, "steps": 2}
    assert restored.api == job.api
    assert restored.models == job.models
    assert restored.outputs == job.outputs
    assert restored.warnings == ["kept"]
    assert restored.status == "succeeded"
    assert restored.prompt_id == "p1"
    assert restored.workflow_hash == "sha256:abc"
    assert restored.idempotency_key == "key-abc123"


def test_record_is_plain_json() -> None:
    encoded = json.dumps(to_record(make_job()))
    assert '"schema": 1' in encoded


def test_from_record_rejects_other_schemas() -> None:
    record = to_record(make_job())
    record["schema"] = 2
    with pytest.raises(ManifestError, match="unsupported schema"):
        from_record(record, source="runs/x.json")


def test_from_record_rejects_missing_required_fields() -> None:
    record = to_record(make_job())
    record["job_id"] = ""
    with pytest.raises(ManifestError, match="missing required field"):
        from_record(record)


def test_from_record_rejects_non_object() -> None:
    with pytest.raises(ManifestError, match="expected a JSON object"):
        from_record(["nope"])  # type: ignore[arg-type]


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "runs")
    path = store.write(make_job())

    assert path.exists()
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert list(path.parent.glob("*.tmp")) == []
    assert list(path.parent.glob(".*")) == []


def test_read_back_after_write(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "runs")
    job = make_job(status="timeout")
    store.write(job)

    restored = store.read(job.job_id)
    assert restored is not None
    assert restored.status == "timeout"
    assert store.read("missing") is None


def test_load_returns_newest_first_and_respects_keep(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "runs", keep=2)
    for index, job_id in enumerate(["old", "mid", "new"]):
        path = store.write(make_job(job_id))
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    loaded = store.load()
    assert [job.job_id for job in loaded] == ["new", "mid"]


def test_load_skips_corrupt_files_instead_of_failing(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "runs")
    store.write(make_job("good"))
    (tmp_path / "runs" / "bad.json").write_text("{not json", encoding="utf-8")

    loaded = store.load()
    assert [job.job_id for job in loaded] == ["good"]
    assert len(store.skipped) == 1


def test_load_of_missing_directory_is_empty(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "nope")
    assert store.load() == []
    assert len(store) == 0


def test_prune_deletes_the_oldest_beyond_keep(tmp_path: Path) -> None:
    store = ManifestStore(tmp_path / "runs", keep=1)
    for index, job_id in enumerate(["a", "b", "c"]):
        os.utime(store.write(make_job(job_id)), (1_700_000_000 + index,) * 2)

    assert store.prune() == 2
    assert [path.stem for path in (tmp_path / "runs").glob("*.json")] == ["c"]
    assert store.prune() == 0
