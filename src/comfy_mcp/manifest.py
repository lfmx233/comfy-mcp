"""Run manifests on disk: one JSON file per job under runs/.

The file exists for reproducibility, not for logging. It holds the converted API
prompt, the resolved params, the workflow hash, and the model list, so a run can be
explained months later even after the sidecar changed underneath it.

Writes are atomic: a temp file in the same directory, fsync, then os.replace(). A
half-written manifest is worse than no manifest at all, because the next restart
would rehydrate it and report a job whose prompt was never sent.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .jobs import Job

SCHEMA = 1
KEEP = 100
_FIELDS = (
    "job_id",
    "workflow",
    "workflow_hash",
    "instance",
    "status",
    "progress",
    "prompt_id",
    "idempotency_key",
    "params",
    "api",
    "models",
    "error",
    "outputs",
    "warnings",
    "created_at",
    "started_at",
    "finished_at",
)


class ManifestError(RuntimeError):
    """A manifest file exists but cannot be trusted."""


def to_record(job: Job) -> dict[str, Any]:
    """Serialize a job into the on-disk shape (schema-tagged on purpose)."""
    record: dict[str, Any] = {"schema": SCHEMA}
    for name in _FIELDS:
        record[name] = getattr(job, name)
    return record


def from_record(record: dict[str, Any], *, source: str = "") -> Job:
    """Rebuild a job, rejecting anything we do not understand rather than guessing."""
    where = f"{source}: " if source else ""
    if not isinstance(record, dict):
        raise ManifestError(f"{where}expected a JSON object")
    schema = record.get("schema")
    if schema != SCHEMA:
        raise ManifestError(f"{where}unsupported schema {schema!r} (this build reads {SCHEMA})")
    missing = [name for name in ("job_id", "workflow", "instance") if not record.get(name)]
    if missing:
        raise ManifestError(f"{where}missing required field(s): {', '.join(missing)}")
    return Job(
        job_id=str(record["job_id"]),
        workflow=str(record["workflow"]),
        params=dict(record.get("params") or {}),
        instance=str(record["instance"]),
        status=record.get("status") or "queued",
        progress=float(record.get("progress") or 0.0),
        prompt_id=record.get("prompt_id"),
        workflow_hash=str(record.get("workflow_hash") or ""),
        idempotency_key=record.get("idempotency_key"),
        error=record.get("error"),
        outputs=list(record.get("outputs") or ()),
        warnings=list(record.get("warnings") or ()),
        api=dict(record.get("api") or {}),
        models=list(record.get("models") or ()),
        created_at=float(record.get("created_at") or 0.0),
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
    )


class ManifestStore:
    """The runs/ directory: read it once at startup, write through on every finish."""

    def __init__(self, directory: Path, *, keep: int = KEEP) -> None:
        self.directory = Path(directory)
        self.keep = max(1, keep)
        self.skipped: list[str] = []

    def __len__(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(1 for _ in self.directory.glob("*.json"))

    def path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.json"

    def write(self, job: Job) -> Path:
        """Write one manifest atomically and return its path."""
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(job.job_id)
        payload = json.dumps(to_record(job), ensure_ascii=False, indent=1)
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=f".{job.job_id}-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            with suppress(OSError):
                tmp.unlink()
            raise
        return target

    def read(self, job_id: str) -> Job | None:
        path = self.path(job_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestError(f"{path}: {exc}") from exc
        return from_record(raw, source=str(path))

    def load(self) -> list[Job]:
        """Rehydrate the newest manifests, newest first.

        Unreadable files are skipped and recorded in self.skipped instead of
        killing startup: one corrupt file must not cost you the other 99 runs.
        """
        self.skipped = []
        if not self.directory.exists():
            return []
        jobs: list[Job] = []
        try:
            paths = sorted(
                self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError as exc:
            self.skipped.append(f"{self.directory}: {exc}")
            return []
        for path in paths:
            if len(jobs) >= self.keep:
                break
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                jobs.append(from_record(raw, source=str(path)))
            except (OSError, ValueError, ManifestError) as exc:
                self.skipped.append(str(exc))
        return jobs

    def prune(self) -> int:
        """Delete the oldest manifests beyond the keep limit; return how many went."""
        if not self.directory.exists():
            return 0
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for path in paths[self.keep :]:
            with suppress(OSError):
                path.unlink()
                removed += 1
        return removed
