"""The MCP surface: six tools that wrap one guarded ComfyUI instance.

Design rule: every tool either returns data or returns the error envelope from
`errors.py` -- never a raw exception, because the caller is a model, not a human.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import time
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.types import CallToolResult, TextContent

from . import __version__
from .client import ComfyClient, ComfyUnreachable
from .config import ConfigError, Instance, Settings, load_settings
from .errors import classify_execution_error, error, from_exception
from .graph import prompt_hash, validate_prompt
from .hashes import ModelHasher
from .jobs import Job, JobStore, Progress
from .manifest import ManifestStore
from .registry import (
    Registry,
    RegistryError,
    apply_params,
    load_registry,
    resolve_params,
)
from .resources import image_block, image_size, resource_link

log = logging.getLogger(__name__)

INSTRUCTIONS = """You are talking to a curated ComfyUI bridge, not to raw ComfyUI.

Start with comfy_status to see whether the instance is alive, then
comfy_list_workflows to see what it will run. Only registered workflows can run, and
only parameters declared in their sidecar can change -- anything else is rejected on
purpose, because an unregistered parameter change is an unreproducible run.

Typical loop:
1. comfy_list_workflows  -> pick a name, read its params (types, ranges, defaults)
2. comfy_validate        -> free preflight: missing nodes, missing models, bad params
3. comfy_run             -> blocks until done, streams progress, returns a manifest
4. comfy_job / comfy_cancel -> only needed after a TIMEOUT, which returns a job_id

A manifest with a workflow_hash plus the resolved params is enough to reproduce an
image, so keep it. On failure, read `code` first: retryable=true means the same call
can work later (QUEUE_FULL, TIMEOUT, CUDA_OOM, INSTANCE_DOWN); retryable=false means a
parameter, a model, or the graph itself must change.
"""

HEARTBEAT_S = 15.0
POLL_S = 1.0
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")


class _Fail(Exception):
    """Internal: unwinds a tool body with an error envelope."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message", ""))
        self.payload = payload


def _returns_result(fn: Any) -> bool:
    """True when the tool hands back content blocks instead of a plain dict."""
    annotation = inspect.signature(fn).return_annotation
    if isinstance(annotation, str):
        return "CallToolResult" in annotation
    return annotation is CallToolResult


def _envelope(payload: dict[str, Any]) -> CallToolResult:
    """An error envelope that still carries structured data for machine callers."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=True,
    )


def _guarded(fn: Any) -> Any:
    """Turn internal failures into the error envelope before the SDK sees them.

    The SDK turns any uncaught exception into an opaque tool crash, which is exactly
    what this server promises never to do.
    """

    returns_result = _returns_result(fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except _Fail as exc:
            return _envelope(exc.payload) if returns_result else exc.payload
        except Exception as exc:  # noqa: BLE001 - last-resort mapping
            log.exception("tool %s crashed", fn.__name__)
            payload = from_exception(exc)
            return _envelope(payload) if returns_result else payload

    return wrapper


class App:
    """Server state: settings, the job table, and one lazily loaded registry per instance."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs = JobStore()
        self.manifests = ManifestStore(settings.runs_dir, keep=settings.keep_runs)
        self.hasher = ModelHasher(settings.models_dir, settings.runs_dir / "checksums.json")
        self._registries: dict[str, tuple[Registry, dict[str, Any]]] = {}
        self._recover()

    def _recover(self) -> None:
        """Rehydrate the runs/ directory on startup.

        A TIMEOUT that outlives the process is still a job: the model that holds its id
        must be able to ask again after a restart, so the table is rebuilt before the
        first tool call, not lazily on a miss.
        """
        recovered = self.manifests.load()
        adopted = self.jobs.adopt(recovered)
        for problem in self.manifests.skipped:
            log.warning("skipping unreadable manifest: %s", problem)
        if adopted:
            log.info("recovered %d job manifest(s) from %s", adopted, self.settings.runs_dir)

    async def record(self, job: Job) -> None:
        """Fill in model digests, then persist the job. Never fatal."""
        if not job.models:
            job.models = _models(job.api)
        for model in job.models:
            if model.get("sha256") is None:
                name = str(model.get("name") or "")
                model["sha256"] = await asyncio.to_thread(self.hasher.sha256, name)
        try:
            self.manifests.write(job)
            self.manifests.prune()
        except OSError as exc:
            log.warning("could not persist job %s: %s", job.job_id, exc)

    def resolve(self, instance: str | None) -> Instance:
        try:
            return self.settings.instance(instance)
        except ConfigError as exc:
            raise _Fail(error("INVALID_PARAM", str(exc))) from exc

    async def registry(self, inst: Instance) -> tuple[Registry, dict[str, Any]]:
        cached = self._registries.get(inst.name)
        if cached is not None:
            return cached
        try:
            async with ComfyClient(inst.http_base) as client:
                node_defs = await client.object_info()
        except (ComfyUnreachable, Exception) as exc:  # noqa: BLE001 - mapped below
            raise _Fail(from_exception(exc)) from exc
        try:
            registry = load_registry(self.settings.workflows_dir, node_defs)
        except RegistryError as exc:
            raise _Fail(error(exc.code, str(exc))) from exc
        self._registries[inst.name] = (registry, node_defs)
        return registry, node_defs

    def workflow_dir_count(self) -> int:
        directory = self.settings.workflows_dir
        if not directory.exists():
            return 0
        return sum(1 for p in directory.iterdir() if p.suffix.lower() in {".yaml", ".yml"})


def _run_result(job: Job, *, reused: bool = False) -> dict[str, Any]:
    manifest = {
        "workflow": job.workflow,
        "workflow_hash": job.workflow_hash,
        "params": job.params,
        "models": job.models or _models(job.api),
        "instance": job.instance,
        "started_at": job.started_at,
    }
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        # The prompt id is what comfy://output/<prompt_id>/<index> is keyed on, so a
        # caller that only reads the text block can still reach the full-size image.
        "prompt_id": job.prompt_id,
        "status": job.status,
        "elapsed_s": job.elapsed_s,
        "manifest": manifest,
        "outputs": job.outputs,
        "warnings": job.warnings,
    }
    if reused:
        payload["reused"] = True
    if job.error is not None:
        payload["error"] = job.error
    return payload


def _models(api: dict[str, Any]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for node_id, node in api.items():
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, str) and value.lower().endswith(MODEL_SUFFIXES):
                models.append({"node": node_id, "name": value, "sha256": None})
    return models


def _outputs(entry: dict[str, Any], prompt_id: str | None) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for node_id, payload in (entry.get("outputs") or {}).items():
        for image in payload.get("images") or ():
            index = len(outputs)
            outputs.append(
                {
                    "index": index,
                    "node": node_id,
                    "kind": "image",
                    "filename": image.get("filename"),
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                    "width": image.get("width"),
                    "height": image.get("height"),
                    "resource": f"comfy://output/{prompt_id}/{index}",
                }
            )
    return outputs


async def _attach_outputs(client: ComfyClient, job: Job, *, limit: int = 8) -> bytes | None:
    """Fetch the first few outputs to learn their real size, returning the first image.

    /history does not report width or height, so the only honest source is the image
    itself. Fetching up to eight of them over the local network is cheap; fetching
    them only to fill a field for a remote instance would not be, hence the cap.
    """

    async def one(output: dict[str, Any]) -> tuple[int, bytes | None]:
        filename = output.get("filename")
        if not filename:
            return int(output.get("index") or 0), None
        try:
            data = await client.view(
                str(filename),
                subfolder=str(output.get("subfolder") or ""),
                type_=str(output.get("type") or "output"),
            )
        except Exception as exc:  # noqa: BLE001 - a missing image is not a failed run
            log.debug("could not fetch %s: %s", filename, exc)
            return int(output.get("index") or 0), None
        size = image_size(data)
        if size is not None:
            output["width"], output["height"] = size
        return int(output.get("index") or 0), data

    fetched = await asyncio.gather(*(one(o) for o in job.outputs[:limit]))
    for index, data in fetched:
        if index == 0:
            return data
    return None


def _content_result(
    app: App, job: Job, *, first_image: bytes | None = None, reused: bool = False
) -> CallToolResult:
    """Build the content blocks: JSON text, at most one image, one link per output."""
    payload = _run_result(job, reused=reused)
    content: list[Any] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    if first_image is not None:
        content.append(image_block(first_image, max_edge=app.settings.preview_max_edge))
    for output in job.outputs:
        if output.get("filename") and job.prompt_id:
            content.append(resource_link(job.prompt_id, int(output["index"]), output))
    return CallToolResult(content=content, structured_content=payload)


async def _complete(
    app: App,
    job: Job,
    *,
    client: ComfyClient | None = None,
    want_preview: bool = False,
) -> CallToolResult:
    """Persist the terminal state, then shape the response."""
    first: bytes | None = None
    if client is not None and want_preview and job.status == "succeeded" and job.outputs:
        first = await _attach_outputs(client, job)
    await app.record(job)
    return _content_result(app, job, first_image=first)


def _execution_message(entry: dict[str, Any]) -> str:
    status = entry.get("status") or {}
    for message in status.get("messages") or ():
        if (
            isinstance(message, (list, tuple))
            and len(message) == 2
            and message[0] == "execution_error"
        ):
            data = message[1] or {}
            node = data.get("node_id", "?")
            kind = data.get("node_type", "?")
            text = data.get("exception_message") or "execution failed"
            return f"node {node} ({kind}): {text}"
    return "ComfyUI reported status_str=error without an execution_error message"


def _issues_payload(issues: list[Any]) -> dict[str, Any]:
    """Collapse preflight issues into one envelope, keeping every detail in the message."""
    priority = ("NODE_MISSING", "MODEL_MISSING", "INVALID_PARAM", "INPUT_MISSING", "OUTPUT_MISSING")
    codes = [issue.code for issue in issues]
    code = next((c for c in priority if c in codes), codes[0] if codes else "INVALID_PARAM")
    lines = [f"{issue.code}: {issue.message}" for issue in issues]
    return error(code, "; ".join(lines[:5]), hint=lines[0] if lines else None)


async def _report(ctx: Context | None, job: Job) -> None:
    if ctx is None:
        return
    try:
        await ctx.report_progress(job.progress, 100.0, f"{job.workflow}: {job.progress:.0f}%")
    except Exception:  # noqa: BLE001 - progress is best-effort, never fatal
        log.debug("progress reporting unavailable", exc_info=True)


def _terminal_event(event: dict[str, Any]) -> bool:
    kind = event.get("type")
    if kind in {"execution_success", "execution_error", "execution_interrupted"}:
        return True
    return kind == "executing" and (event.get("data") or {}).get("node") is None


async def _history_entry(client: ComfyClient, prompt_id: str | None) -> dict[str, Any] | None:
    if not prompt_id:
        return None
    history = await client.history(prompt_id)
    entry = history.get(prompt_id)
    return entry if isinstance(entry, dict) else None


def _finished(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    status = entry.get("status") or {}
    if status.get("status_str") in {"success", "error"}:
        return True
    return bool(status.get("completed"))


async def _pump(
    stream: Any,
    tracker: Progress,
    job: Job,
    done: asyncio.Event,
    client: ComfyClient,
    ctx: Context | None,
) -> None:
    """Feed websocket events into the tracker; fall back to polling if the socket dies."""
    iterator = stream.__aiter__()
    try:
        while True:
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=HEARTBEAT_S)
            except TimeoutError:
                # silent socket: ask history whether the prompt finished behind our back
                if _finished(await _history_entry(client, job.prompt_id)):
                    done.set()
                    return
                continue
            except StopAsyncIteration:
                break
            job.progress = tracker.feed(event)
            await _report(ctx, job)
            if _terminal_event(event):
                done.set()
                return
    except asyncio.CancelledError:
        raise
    except (ComfyUnreachable, OSError) as exc:
        log.debug("event stream broke: %s", exc)
    while not done.is_set():
        if _finished(await _history_entry(client, job.prompt_id)):
            done.set()
            return
        await asyncio.sleep(POLL_S)


async def _abort(client: ComfyClient, prompt_id: str | None) -> None:
    """Best-effort stop: interrupt the running node, then drop the prompt from the queue."""
    with contextlib.suppress(Exception):
        await client.interrupt()
    if prompt_id:
        with contextlib.suppress(Exception):
            await client.delete_from_queue([prompt_id])


def build_server(settings: Settings | None = None) -> MCPServer:
    app = App(settings or load_settings())
    server = MCPServer(name="comfy-mcp", version=__version__, instructions=INSTRUCTIONS)

    @server.tool()
    @_guarded
    async def comfy_status(instance: str | None = None) -> dict[str, Any]:
        """Is the instance alive, what is queued, and how much VRAM is left."""
        inst = app.resolve(instance)
        async with ComfyClient(inst.http_base) as client:
            status = await client.status(inst.name)
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
            "queue": {"running": len(status.queue.running), "pending": len(status.queue.pending)},
            "workflows": app.workflow_dir_count(),
            "max_queue": app.settings.max_queue,
            "error": status.error,
            "code": None if status.reachable else "INSTANCE_DOWN",
        }

    @server.tool()
    @_guarded
    async def comfy_list_workflows(
        tag: str | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """List the registered workflows and their parameter schemas."""
        inst = app.resolve(instance)
        registry, _ = await app.registry(inst)
        workflows = [spec.info() for spec in registry.list(tag)]
        return {"instance": inst.name, "count": len(workflows), "workflows": workflows}

    @server.tool()
    @_guarded
    async def comfy_validate(
        workflow: str, params: dict[str, Any] | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """Preflight a run against the instance without submitting anything."""
        inst = app.resolve(instance)
        registry, node_defs = await app.registry(inst)
        try:
            spec = registry.get(workflow)
        except RegistryError as exc:
            raise _Fail(error(exc.code, str(exc))) from exc
        param_errors: list[str] = []
        api: dict[str, Any] = {}
        if params:
            try:
                resolved = resolve_params(spec, params)
                api = apply_params(spec, resolved)
            except RegistryError as exc:
                param_errors.append(str(exc))
        else:
            api = apply_params(spec, {})
        issues = validate_prompt(api, node_defs)
        missing_nodes = sorted({i.message for i in issues if i.code == "NODE_MISSING"})
        missing_models = sorted({i.message for i in issues if i.code == "MODEL_MISSING"})
        param_errors.extend(
            i.message for i in issues if i.code in {"INVALID_PARAM", "INPUT_MISSING"}
        )
        return {
            "ok": not (missing_nodes or missing_models or param_errors),
            "workflow": spec.name,
            "instance": inst.name,
            "missing_nodes": missing_nodes,
            "missing_models": missing_models,
            "param_errors": param_errors,
            "warnings": list(spec.warnings),
        }

    @server.tool()
    @_guarded
    async def comfy_run(
        workflow: str,
        params: dict[str, Any] | None = None,
        instance: str | None = None,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
        preview: bool = True,
        ctx: Context | None = None,
    ) -> CallToolResult:
        """Run a registered workflow and block until it finishes, times out, or fails.

        Returns a manifest (workflow_hash + resolved params + models) so the image can be
        reproduced. On TIMEOUT the job keeps its id and can be polled with comfy_job.
        """
        inst = app.resolve(instance)
        registry, node_defs = await app.registry(inst)
        try:
            spec = registry.get(workflow)
            resolved = resolve_params(spec, params)
            api = apply_params(spec, resolved)
        except RegistryError as exc:
            raise _Fail(error(exc.code, str(exc))) from exc

        issues = validate_prompt(api, node_defs)
        if issues:
            raise _Fail(_issues_payload(issues))

        key = idempotency_key or prompt_hash(api)
        existing = app.jobs.find_by_idempotency(key)
        if existing is not None:
            # A replay still owes the caller the picture: the whole point of asking for
            # the same params twice is that the second caller wants the same image.
            first: bytes | None = None
            if preview and existing.status == "succeeded" and existing.outputs:
                try:
                    async with ComfyClient(inst.http_base) as client:
                        first = await _attach_outputs(client, existing)
                except Exception as exc:  # noqa: BLE001 - a replay must still answer
                    log.warning("could not re-attach the preview for %s: %s", existing.job_id, exc)
            return _content_result(app, existing, first_image=first, reused=True)

        async with ComfyClient(inst.http_base) as client:
            try:
                queue = await client.queue()
            except Exception as exc:  # noqa: BLE001 - transport failures become envelopes
                raise _Fail(from_exception(exc)) from exc
            depth = len(queue.running) + len(queue.pending)
            if depth >= app.settings.max_queue:
                raise _Fail(
                    error(
                        "QUEUE_FULL",
                        f"{inst.name} has {depth} queued prompts (max {app.settings.max_queue})",
                    )
                )

            timeout = float(timeout_s if timeout_s is not None else app.settings.default_timeout_s)
            job = app.jobs.create(
                workflow=spec.name,
                params=resolved,
                instance=inst.name,
                workflow_hash=spec.graph_hash,
                idempotency_key=key,
                warnings=spec.warnings,
            )
            job.api = api
            tracker = Progress(api.keys())
            client_id = f"comfy-mcp-{job.job_id}"
            done = asyncio.Event()
            pump: asyncio.Task[None] | None = None
            try:
                async with client.event_stream(client_id) as stream:
                    job.prompt_id = await client.submit(api, client_id=client_id)
                    job.status = "running"
                    job.started_at = time.time()
                    pump = asyncio.create_task(_pump(stream, tracker, job, done, client, ctx))
                    try:
                        await asyncio.wait_for(done.wait(), timeout=timeout)
                    except TimeoutError:
                        await _abort(client, job.prompt_id)
                        app.jobs.finish(
                            job,
                            "timeout",
                            error_payload=error(
                                "TIMEOUT",
                                f"{spec.name} did not finish within {timeout:g}s",
                                hint=(
                                    f"poll comfy_job(job_id={job.job_id!r}) or retry "
                                    "with a larger timeout_s"
                                ),
                                job_id=job.job_id,
                            ),
                        )
                        return await _complete(app, job)
                    finally:
                        if pump is not None:
                            pump.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await pump

                if job.status == "canceled":
                    return await _complete(app, job)
                entry = await _history_entry(client, job.prompt_id)
                if entry is None:
                    app.jobs.finish(
                        job,
                        "failed",
                        error_payload=error(
                            "EXECUTION_FAILED",
                            "ComfyUI finished the prompt but /history has no entry for it",
                            job_id=job.job_id,
                        ),
                    )
                    return await _complete(app, job)
                status_str = (entry.get("status") or {}).get("status_str")
                if status_str == "error":
                    message = _execution_message(entry)
                    app.jobs.finish(
                        job,
                        "failed",
                        error_payload=error(
                            classify_execution_error(message), message, job_id=job.job_id
                        ),
                    )
                else:
                    app.jobs.finish(job, "succeeded", outputs=_outputs(entry, job.prompt_id))
                return await _complete(app, job, client=client, want_preview=preview)
            except Exception as exc:  # noqa: BLE001 - transport failures become envelopes
                app.jobs.finish(
                    job, "failed", error_payload=error("INSTANCE_DOWN", str(exc), job_id=job.job_id)
                )
                raise _Fail(from_exception(exc)) from exc

    @server.tool()
    @_guarded
    async def comfy_job(job_id: str) -> dict[str, Any]:
        """Look up a job by id: status, progress, elapsed time, and outputs when done."""
        job = app.jobs.get(job_id)
        if job is None:
            raise _Fail(
                error(
                    "INVALID_PARAM",
                    f"unknown job {job_id!r}",
                    hint="use the job_id returned by comfy_run",
                )
            )
        return job.info()

    @server.tool()
    @_guarded
    async def comfy_cancel(job_id: str) -> dict[str, Any]:
        """Cancel a pending or running job. Already-finished jobs report canceled=false."""
        job = app.jobs.get(job_id)
        if job is None:
            raise _Fail(
                error("INVALID_PARAM", f"unknown job {job_id!r}", hint="use comfy_job(job_id)")
            )
        if job.terminal:
            return {
                "canceled": False,
                "job_id": job.job_id,
                "reason": f"job already {job.status}",
                "queue_removed": False,
            }
        inst = app.resolve(job.instance)
        removed = bool(job.prompt_id)
        async with ComfyClient(inst.http_base) as client:
            try:
                await _abort(client, job.prompt_id)
            except Exception as exc:  # noqa: BLE001
                raise _Fail(from_exception(exc)) from exc
        app.jobs.finish(
            job,
            "canceled",
            error_payload=error("CANCELED", "canceled by request", job_id=job.job_id),
        )
        return {"canceled": True, "job_id": job.job_id, "queue_removed": removed}

    @server.resource(
        "comfy://output/{prompt_id}/{index}",
        name="comfy-output",
        mime_type="image/png",
        description="One original output image, by prompt id and index (bytes, not base64).",
    )
    async def comfy_output(prompt_id: str, index: str) -> bytes:
        """Read the full-resolution image behind a ResourceLink from comfy_run."""
        job = app.jobs.find_by_prompt(prompt_id)
        if job is None:
            raise ValueError(f"unknown prompt {prompt_id!r}")
        try:
            output = job.outputs[int(index)]
        except (ValueError, IndexError) as exc:
            raise ValueError(f"no output {index!r} for prompt {prompt_id!r}") from exc
        filename = output.get("filename")
        if not filename:
            raise ValueError(f"output {index!r} of {prompt_id!r} has no file")
        inst = app.resolve(job.instance)
        async with ComfyClient(inst.http_base) as client:
            return await client.view(
                str(filename),
                subfolder=str(output.get("subfolder") or ""),
                type_=str(output.get("type") or "output"),
            )

    return server


def main() -> None:  # pragma: no cover - exercised via the CLI
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"comfy-mcp: {exc}") from exc
    build_server(settings).run(transport=settings.transport)
