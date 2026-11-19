"""Workflow registry: a pinned API graph plus a sidecar that declares its parameters.

The registry is the whole safety story of this server. An agent may not hand us a graph;
it may only name a workflow and fill in parameters the sidecar already declared, each
pointing at a concrete `node_id.inputs.name` path. Anything else is rejected before a
single byte reaches ComfyUI, which is what keeps arbitrary-graph execution (and the RCE
surface that comes with custom nodes) off the table.

Loading is strict on purpose: a sidecar that points at a path which does not exist in the
graph, or at a value of the wrong type, fails at startup rather than at 3am mid-run.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .graph import (
    GraphError,
    Issue,
    convert,
    looks_like_api_prompt,
    looks_like_ui_graph,
    prompt_hash,
    validate_prompt,
)

PARAM_TYPES = frozenset({"string", "integer", "float", "boolean"})
SIDECAR_KEYS = frozenset({"name", "description", "tags", "workflow", "params"})
PARAM_KEYS = frozenset(
    {
        "type",
        "path",
        "paths",
        "required",
        "default",
        "minimum",
        "maximum",
        "max_len",
        "enum",
        "description",
    }
)
MAX_SEED = 4294967295


class RegistryError(ValueError):
    """A workflow or one of its parameters is unusable."""

    def __init__(self, code: str, message: str, *, workflow: str | None = None) -> None:
        self.code = code
        self.workflow = workflow
        prefix = f"{code}: {message}"
        if workflow:
            prefix = f"{code}: [{workflow}] {message}"
        super().__init__(prefix)


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One declared parameter and where it lands inside the API prompt."""

    name: str
    type: str
    paths: tuple[str, ...]
    description: str = ""
    required: bool = False
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    max_len: int | None = None
    enum: tuple[Any, ...] = ()

    @property
    def randomize(self) -> bool:
        """`-1` on an integer param means "pick a seed for me" (documented convention)."""

        return self.type == "integer" and self.default == -1

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.required:
            out["required"] = True
        if self.default is not None:
            out["default"] = self.default
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.max_len is not None:
            out["max_len"] = self.max_len
        if self.enum:
            out["enum"] = list(self.enum)
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """A converted, hashed workflow plus its parameter whitelist."""

    name: str
    description: str
    tags: tuple[str, ...]
    api: Mapping[str, Any]
    graph_hash: str
    params: Mapping[str, ParamSpec]
    source: Path
    warnings: tuple[str, ...] = ()

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "graph_hash": self.graph_hash,
            "params": {name: spec.info() for name, spec in self.params.items()},
        }


@dataclass(frozen=True, slots=True)
class Registry:
    """Every workflow the server is willing to run, keyed by name."""

    workflows: Mapping[str, WorkflowSpec] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.workflows)

    def names(self) -> list[str]:
        return sorted(self.workflows)

    def get(self, name: str) -> WorkflowSpec:
        try:
            return self.workflows[name]
        except KeyError:
            known = ", ".join(sorted(self.workflows)) or "none"
            raise RegistryError(
                "UNKNOWN_WORKFLOW",
                f"unknown workflow {name!r}; registered: {known}",
            ) from None

    def list(self, tag: str | None = None) -> list[WorkflowSpec]:
        specs = [self.workflows[name] for name in self.names()]
        if tag:
            specs = [spec for spec in specs if tag in spec.tags]
        return specs


# --------------------------------------------------------------------------- params


def _fail(code: str, message: str, workflow: str | None = None) -> RegistryError:
    return RegistryError(code, message, workflow=workflow)


def _as_param(name: str, raw: Any, workflow: str) -> ParamSpec:
    if not isinstance(raw, Mapping):
        raise _fail("REGISTRY_INVALID", f"param {name!r} must be a mapping", workflow)
    unknown = set(raw) - PARAM_KEYS
    if unknown:
        raise _fail(
            "REGISTRY_INVALID",
            f"param {name!r} has unknown key(s) {sorted(unknown)}; allowed: {sorted(PARAM_KEYS)}",
            workflow,
        )

    kind = raw.get("type")
    if kind not in PARAM_TYPES:
        raise _fail(
            "REGISTRY_INVALID",
            f"param {name!r} type {kind!r} is not one of {sorted(PARAM_TYPES)}",
            workflow,
        )

    single, plural = raw.get("path"), raw.get("paths")
    if bool(single) == bool(plural):
        raise _fail(
            "REGISTRY_INVALID",
            f"param {name!r} needs exactly one of 'path' or 'paths'",
            workflow,
        )
    paths = (single,) if single else tuple(plural)
    if not all(isinstance(path, str) and path for path in paths):
        raise _fail("REGISTRY_INVALID", f"param {name!r} has a non-string path", workflow)

    enum = raw.get("enum") or ()
    if not isinstance(enum, (list, tuple)):
        raise _fail("REGISTRY_INVALID", f"param {name!r} enum must be a list", workflow)

    def number(key: str) -> float | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _fail("REGISTRY_INVALID", f"param {name!r} {key} must be a number", workflow)
        return value

    max_len = raw.get("max_len")
    if max_len is not None and (not isinstance(max_len, int) or isinstance(max_len, bool)):
        raise _fail("REGISTRY_INVALID", f"param {name!r} max_len must be an integer", workflow)

    return ParamSpec(
        name=name,
        type=kind,
        paths=paths,
        description=str(raw.get("description") or ""),
        required=bool(raw.get("required")),
        default=raw.get("default"),
        minimum=number("minimum"),
        maximum=number("maximum"),
        max_len=max_len,
        enum=tuple(enum),
    )


def _check_default(spec: ParamSpec, workflow: str) -> None:
    if spec.default is None:
        return
    if spec.randomize and spec.default == -1:
        return
    try:
        _coerce(spec, spec.default, workflow)
    except RegistryError:
        raise _fail(
            "REGISTRY_INVALID",
            f"param {spec.name!r} default {spec.default!r} is not a valid {spec.type}",
            workflow,
        ) from None


def _coerce(spec: ParamSpec, value: Any, workflow: str) -> Any:
    """Type/enum/range check for one value; returns the value to write into the prompt."""

    kind = spec.type
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _fail(
                "INVALID_PARAM",
                f"{spec.name!r} expects a boolean, got {type(value).__name__}",
                workflow,
            )
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(
                "INVALID_PARAM",
                f"{spec.name!r} expects an integer, got {value!r}",
                workflow,
            )
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail("INVALID_PARAM", f"{spec.name!r} expects a number, got {value!r}", workflow)
        value = float(value)
    else:  # string
        if not isinstance(value, str):
            raise _fail(
                "INVALID_PARAM",
                f"{spec.name!r} expects a string, got {type(value).__name__}",
                workflow,
            )
        if spec.max_len is not None and len(value) > spec.max_len:
            raise _fail(
                "INVALID_PARAM",
                f"{spec.name!r} is {len(value)} characters, over the {spec.max_len} limit",
                workflow,
            )

    if spec.enum and value not in spec.enum:
        raise _fail(
            "INVALID_PARAM",
            f"{spec.name!r} must be one of {list(spec.enum)}, got {value!r}",
            workflow,
        )
    if spec.minimum is not None and value < spec.minimum:
        raise _fail(
            "INVALID_PARAM", f"{spec.name!r} must be >= {spec.minimum}, got {value!r}", workflow
        )
    if spec.maximum is not None and value > spec.maximum:
        raise _fail(
            "INVALID_PARAM", f"{spec.name!r} must be <= {spec.maximum}, got {value!r}", workflow
        )
    return value


def resolve_params(
    spec: WorkflowSpec,
    params: Mapping[str, Any] | None,
    *,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Validate caller params against the whitelist and return the values to inject.

    Unknown keys are an error, not a warning: silently ignoring `steps=40` because the
    sidecar calls it `num_steps` would make runs look reproducible when they are not.
    """

    params = dict(params or {})
    unknown = set(params) - set(spec.params)
    if unknown:
        declared = ", ".join(sorted(spec.params)) or "none"
        raise _fail(
            "INVALID_PARAM",
            f"unknown parameter(s) {sorted(unknown)}; this workflow declares: {declared}",
            spec.name,
        )

    generator = rng or random.Random()
    resolved: dict[str, Any] = {}
    for name, param in spec.params.items():
        if name in params:
            value = params[name]
        elif param.default is not None:
            value = param.default
        elif param.required:
            raise _fail("INVALID_PARAM", f"missing required parameter {name!r}", spec.name)
        else:
            continue
        if param.randomize and value == -1:
            value = generator.randint(0, MAX_SEED)
        else:
            value = _coerce(param, value, spec.name)
        resolved[name] = value
    return resolved


def _lookup(api: Mapping[str, Any], path: str, workflow: str) -> Any:
    parts = path.split(".")
    if len(parts) < 3:
        raise _fail(
            "REGISTRY_INVALID",
            f"param path {path!r} must look like '3.inputs.seed'",
            workflow,
        )
    node_id, rest = parts[0], parts[1:]
    node = api.get(node_id)
    if not isinstance(node, Mapping):
        raise _fail(
            "REGISTRY_INVALID",
            f"param path {path!r}: node {node_id!r} is not in the graph",
            workflow,
        )
    cursor: Any = node
    for key in rest:
        if not isinstance(cursor, Mapping) or key not in cursor:
            raise _fail(
                "REGISTRY_INVALID",
                f"param path {path!r}: {key!r} missing under {path[: path.rfind('.')]!r}",
                workflow,
            )
        cursor = cursor[key]
    return cursor


def _check_path_types(spec: WorkflowSpec) -> None:
    for param in spec.params.values():
        for path in param.paths:
            current = _lookup(spec.api, path, spec.name)
            if not _kind_matches(param.type, current):
                raise _fail(
                    "REGISTRY_INVALID",
                    f"param {param.name!r} ({param.type}) points at {path}, which holds "
                    f"{current!r} ({type(current).__name__})",
                    spec.name,
                )


def _kind_matches(kind: str, value: Any) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def apply_params(
    spec: WorkflowSpec,
    resolved: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deep copy of the pinned graph with the resolved values written in."""

    api = json.loads(json.dumps(spec.api))
    for name, value in resolved.items():
        param = spec.params[name]
        for path in param.paths:
            parts = path.split(".")
            cursor: Any = api
            for key in parts[:-1]:
                cursor = cursor[key]
            cursor[parts[-1]] = value
    return api


# --------------------------------------------------------------------------- loading


def _read_workflow_graph(path: Path, node_defs: Mapping[str, Any]) -> tuple[dict, tuple[str, ...]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if looks_like_api_prompt(raw):
        return dict(raw), ()
    if not looks_like_ui_graph(raw):
        raise _fail(
            "REGISTRY_INVALID",
            f"{path.name} is neither a UI workflow nor an API prompt",
        )
    conversion = convert(raw, node_defs)
    return conversion.api, conversion.warnings


def load_workflow(sidecar_path: Path, node_defs: Mapping[str, Any]) -> WorkflowSpec:
    """Load one `<name>.yaml` sidecar plus the workflow file it names."""

    try:
        raw = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise _fail("REGISTRY_INVALID", f"{sidecar_path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise _fail("REGISTRY_INVALID", f"{sidecar_path.name}: expected a mapping at top level")

    unknown = set(raw) - SIDECAR_KEYS
    if unknown:
        raise _fail(
            "REGISTRY_INVALID",
            f"{sidecar_path.name}: unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(SIDECAR_KEYS)}",
        )

    name = raw.get("name") or sidecar_path.stem
    if not isinstance(name, str) or not name:
        raise _fail("REGISTRY_INVALID", f"{sidecar_path.name}: 'name' must be a string")
    if name != sidecar_path.stem:
        raise _fail(
            "REGISTRY_INVALID",
            f"{sidecar_path.name}: name {name!r} must match the file stem {sidecar_path.stem!r}",
            name,
        )

    graph_name = raw.get("workflow") or f"{name}.json"
    graph_path = (sidecar_path.parent / str(graph_name)).resolve()
    if not graph_path.is_file():
        raise _fail("REGISTRY_INVALID", f"workflow file {graph_name!r} not found", name)

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _fail("REGISTRY_INVALID", "description is required: it is what the agent reads", name)

    tags_raw = raw.get("tags") or []
    if not isinstance(tags_raw, (list, tuple)) or not all(isinstance(t, str) for t in tags_raw):
        raise _fail("REGISTRY_INVALID", "tags must be a list of strings", name)

    params_raw = raw.get("params") or {}
    if not isinstance(params_raw, Mapping):
        raise _fail("REGISTRY_INVALID", "params must be a mapping", name)
    params = {param: _as_param(param, body, name) for param, body in params_raw.items()}
    for param in params.values():
        _check_default(param, name)

    try:
        api, warnings = _read_workflow_graph(graph_path, node_defs)
    except GraphError as exc:
        raise _fail("REGISTRY_INVALID", f"{graph_name}: {exc}", name) from exc

    spec = WorkflowSpec(
        name=name,
        description=description.strip(),
        tags=tuple(tags_raw),
        api=api,
        graph_hash=f"sha256:{prompt_hash(api)}",
        params=params,
        source=sidecar_path,
        warnings=warnings,
    )
    _check_path_types(spec)
    return spec


def load_registry(workflows_dir: Path | str, node_defs: Mapping[str, Any]) -> Registry:
    """Load every sidecar in `workflows_dir`; any broken one fails the whole startup."""

    directory = Path(workflows_dir)
    if not directory.is_dir():
        return Registry()
    workflows: dict[str, WorkflowSpec] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".yaml", ".yml"} or path.name.startswith("."):
            continue
        spec = load_workflow(path, node_defs)
        if spec.name in workflows:
            raise _fail(
                "REGISTRY_INVALID",
                f"duplicate workflow name {spec.name!r} in {path.name} and "
                f"{workflows[spec.name].source.name}",
                spec.name,
            )
        workflows[spec.name] = spec
    return Registry(workflows=workflows)


def validate_workflow(spec: WorkflowSpec, node_defs: Mapping[str, Any]) -> list[Issue]:
    """Preflight the pinned graph against a live `/object_info` payload."""

    return validate_prompt(spec.api, node_defs)
