"""The error envelope: one table, one shape, ten codes.

Every tool reports failure the same way: {code, message, hint, retryable, job_id}.
An agent branches on `code` and acts on `hint`; it never has to parse prose.
"""

from __future__ import annotations

from typing import Any

from .client import ComfyHTTPError, ComfyUnreachable

# code -> (retryable, default hint shown to the model)
ERRORS: dict[str, tuple[bool, str]] = {
    "INVALID_PARAM": (
        False,
        "Read the parameter schema from comfy_list_workflows and pass a value of the "
        "right type and range.",
    ),
    "UNKNOWN_WORKFLOW": (
        False,
        "Call comfy_list_workflows to see the registered workflow names.",
    ),
    "NODE_MISSING": (
        False,
        "This instance lacks the node class; pick another workflow or ask the user to install it.",
    ),
    "MODEL_MISSING": (
        False,
        "Override the model parameter with a file the instance has, or pick another instance.",
    ),
    "QUEUE_FULL": (
        True,
        "Wait for the queue to drain (comfy_status) and retry.",
    ),
    "TIMEOUT": (
        True,
        "Raise timeout_s, or lower steps / resolution and retry. The job_id can be polled.",
    ),
    "CUDA_OOM": (
        True,
        "Retry with a smaller resolution, batch_size 1, or a lighter checkpoint.",
    ),
    "INSTANCE_DOWN": (
        True,
        "Check the instance with comfy_status; retry once it is reachable.",
    ),
    "CANCELED": (
        False,
        "Nothing to do; call comfy_run again if the work is still wanted.",
    ),
    "EXECUTION_FAILED": (
        False,
        "The graph ran but a node raised: read message for the node and the exception.",
    ),
}

_OOM_MARKERS = (
    "out of memory",
    "cuda oom",
    "allocation on device",
    "outofmemoryerror",
)


def error(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    job_id: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """Build the failure envelope; unknown codes are treated as non-retryable."""
    default_retryable, default_hint = ERRORS.get(code, (False, ""))
    return {
        "code": code,
        "message": message,
        "hint": default_hint if hint is None else hint,
        "retryable": default_retryable if retryable is None else retryable,
        "job_id": job_id,
    }


def from_exception(exc: BaseException) -> dict[str, Any]:
    """Map transport failures onto the envelope."""
    if isinstance(exc, ComfyUnreachable):
        return error("INSTANCE_DOWN", str(exc))
    if isinstance(exc, ComfyHTTPError):
        body = (exc.body or "").strip()
        if exc.status >= 500:
            return error("INSTANCE_DOWN", f"ComfyUI returned HTTP {exc.status}: {body[:300]}")
        return error("INVALID_PARAM", f"ComfyUI rejected the request ({exc.status}): {body[:300]}")
    return error("EXECUTION_FAILED", f"{type(exc).__name__}: {exc}")


def classify_execution_error(message: str | None) -> str:
    """Distinguish an OOM (retryable, actionable) from a generic node crash."""
    text = (message or "").lower()
    if any(marker in text for marker in _OOM_MARKERS):
        return "CUDA_OOM"
    return "EXECUTION_FAILED"
