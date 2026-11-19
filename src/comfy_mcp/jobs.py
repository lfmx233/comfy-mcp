"""Job records and the progress math that keeps them monotonically honest.

The table itself is memory; durability is manifest.py's job (runs/<job_id>.json,
rehydrated on startup). Keeping them apart means the state machine stays testable
without touching a filesystem.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

JobStatus = Literal["queued", "running", "succeeded", "failed", "timeout", "canceled"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "timeout", "canceled"})


def _now() -> float:
    return time.time()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


class Progress:
    """Fold ComfyUI's event soup into one monotonic 0-100 number.

    ComfyUI reports per-node progress, so the honest number is
    (finished nodes + fraction of the current node) / total nodes.
    """

    def __init__(self, node_ids: Iterable[str]) -> None:
        self.node_ids = tuple(str(n) for n in node_ids)
        self.total = max(len(self.node_ids), 1)
        self.done: set[str] = set()
        self.current: str | None = None
        self.value = 0.0
        self.max = 0.0
        self.percent = 0.0

    def feed(self, event: dict[str, Any]) -> float:
        kind = event.get("type")
        data = event.get("data") or {}
        node = data.get("node")
        if kind == "execution_cached":
            self.done.update(str(n) for n in data.get("nodes") or ())
        elif kind == "executing":
            if node is None:
                return self.finish()
            self.current = str(node)
            self.value = 0.0
        elif kind == "progress":
            if node is not None:
                self.current = str(node)
            self.value = float(data.get("value") or 0)
            self.max = float(data.get("max") or 0)
        elif kind == "progress_state":
            # ComfyUI 0.3.x also sends one frame carrying *every* node's state.
            # It is the most complete signal there is, so trust it over the deltas.
            self._feed_state(data.get("nodes") or {})
        elif kind in {"executed", "execution_success"}:
            if node is not None:
                self.done.add(str(node))
            self.current = None
            self.value = 0.0
            if kind == "execution_success":
                return self.finish()
        return self._recompute()

    def _feed_state(self, nodes: dict[str, Any]) -> None:
        known = set(self.node_ids)
        running: str | None = None
        for node_id, state in nodes.items():
            if not isinstance(state, dict) or str(node_id) not in known:
                continue
            phase = state.get("state")
            if phase == "finished":
                self.done.add(str(node_id))
            elif phase == "running":
                running = str(node_id)
                self.value = float(state.get("value") or 0)
                self.max = float(state.get("max") or 0)
        self.current = running

    def finish(self) -> float:
        self.percent = 100.0
        return self.percent

    def _recompute(self) -> float:
        fraction = self.value / self.max if self.max > 0 else 0.0
        raw = (len(self.done) + min(fraction, 1.0)) / self.total * 100.0
        # never report 100 before the prompt actually finished, and never go backwards
        self.percent = max(self.percent, min(raw, 99.0))
        return self.percent


@dataclass
class Job:
    job_id: str
    workflow: str
    params: dict[str, Any]
    instance: str
    status: JobStatus = "queued"
    progress: float = 0.0
    prompt_id: str | None = None
    workflow_hash: str = ""
    idempotency_key: str | None = None
    error: dict[str, Any] | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    api: dict[str, Any] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else _now()
        return round(end - self.started_at, 2)

    def info(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "workflow": self.workflow,
            "status": self.status,
            "progress": round(self.progress, 1),
            "elapsed_s": self.elapsed_s,
            "prompt_id": self.prompt_id,
        }
        if self.started_at is not None:
            payload["started_at"] = _iso(self.started_at)
        if self.error is not None:
            payload["error"] = self.error
        if self.outputs:
            payload["outputs"] = self.outputs
        if self.warnings:
            payload["warnings"] = self.warnings
        # The manifest travels with every lookup, not only with the run that made it:
        # after a restart this is the only place a job's inputs still exist.
        payload["manifest"] = {
            "workflow_hash": self.workflow_hash,
            "params": self.params,
            "models": self.models,
            "instance": self.instance,
        }
        return payload


class JobStore:
    """In-memory job table with a short idempotency window."""

    def __init__(self, *, idem_window_s: float = 600.0, keep: int = 100) -> None:
        self.idem_window_s = idem_window_s
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._by_key: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._jobs)

    def create(
        self,
        *,
        workflow: str,
        params: dict[str, Any],
        instance: str,
        workflow_hash: str = "",
        idempotency_key: str | None = None,
        warnings: Iterable[str] = (),
    ) -> Job:
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            workflow=workflow,
            params=dict(params),
            instance=instance,
            workflow_hash=workflow_hash,
            idempotency_key=idempotency_key,
            warnings=list(warnings),
        )
        self._jobs[job.job_id] = job
        if idempotency_key:
            self._by_key[idempotency_key] = job.job_id
        self._trim()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def find_by_prompt(self, prompt_id: str) -> Job | None:
        """Look a job up by ComfyUI's prompt id; used by comfy://output resources."""
        for job in self._jobs.values():
            if job.prompt_id == prompt_id:
                return job
        return None

    def adopt(self, jobs: Iterable[Job]) -> int:
        """Load jobs rehydrated from disk into the live table, keeping the idempotency keys.

        Restart-recovered jobs are how a TIMEOUT survives a crash: the id still
        resolves, and a retry with the same key still reports the original run.
        """
        adopted = 0
        for job in jobs:
            if job.job_id in self._jobs:
                continue
            self._jobs[job.job_id] = job
            if job.idempotency_key:
                self._by_key[job.idempotency_key] = job.job_id
            adopted += 1
        return adopted

    def find_by_idempotency(self, key: str) -> Job | None:
        job_id = self._by_key.get(key)
        job = self._jobs.get(job_id) if job_id else None
        if job is None:
            return None
        if _now() - job.created_at > self.idem_window_s:
            self._by_key.pop(key, None)
            return None
        # Do not cache a retryable outcome: an OOM or a TIMEOUT is the one case where the
        # caller's next move is to try again, and replaying the first error would make the
        # tool look broken. Only finished-success and in-flight jobs are reused.
        if job.status in ("failed", "timeout", "canceled"):
            return None
        return job

    def finish(
        self,
        job: Job,
        status: JobStatus,
        *,
        error_payload: dict[str, Any] | None = None,
        outputs: list[dict[str, Any]] | None = None,
    ) -> Job:
        job.status = status
        job.finished_at = _now()
        if error_payload is not None:
            job.error = error_payload
        if outputs is not None:
            job.outputs = outputs
        if status == "succeeded":
            job.progress = 100.0
        self._trim()
        return job

    def _trim(self) -> None:
        """Drop the oldest finished jobs once the table is over capacity."""
        if len(self._jobs) <= self.keep:
            return
        for job_id, job in sorted(self._jobs.items(), key=lambda kv: kv[1].created_at):
            if len(self._jobs) <= self.keep:
                return
            if not job.terminal:
                continue
            del self._jobs[job_id]
            if job.idempotency_key:
                self._by_key.pop(job.idempotency_key, None)
