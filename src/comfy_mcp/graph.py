"""Graph utilities: UI export -> API prompt, stable hashing, preflight validation.

ComfyUI has two JSON shapes and they are not interchangeable:

* **UI workflow** — what the web frontend saves: `{"nodes": [...], "links": [...]}`
  with canvas positions, a link table and `widgets_values` arrays.
* **API prompt** — what `POST /prompt` accepts: `{node_id: {"class_type": ..., "inputs": {...}}}`
  where every input is a literal value or `[node_id, output_slot]`.

The conversion needs `/object_info` because widget values are **positional** and the
frontend injects hidden widgets the API never sees (`control_after_generate` after a seed,
`dynamicPrompts` after a text box, an upload button after `LoadImage.image`). Those extra
values have no slot in the API prompt, so they must be skipped in the right place.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Frontend-only node types: they carry no work for ComfyUI itself.
VIRTUAL_NODE_TYPES = frozenset(
    {"Reroute", "PrimitiveNode", "Note", "MarkdownNote", "Note Plus (mtb)"}
)
# Types whose value lives in the node body instead of a link.
WIDGET_INPUT_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"})

MODE_ALWAYS = 0
MODE_MUTED = 2
MODE_BYPASS = 4

MODEL_SUFFIXES = (
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".gguf",
    ".sft",
    ".bin",
    ".onnx",
)


class GraphError(ValueError):
    """A UI graph cannot be turned into an API prompt."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        node_id: str | int | None = None,
        node_type: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.node_id = None if node_id is None else str(node_id)
        self.node_type = node_type
        where = ""
        if self.node_id is not None:
            where = f" (node {self.node_id}"
            where += f" {node_type})" if node_type else ")"
        super().__init__(f"{code}: {message}{where}")


@dataclass(frozen=True, slots=True)
class Issue:
    """One preflight problem, mapped to the MCP error code it will become."""

    code: str
    message: str
    hint: str = ""
    node_id: str | None = None
    node_type: str | None = None


@dataclass(frozen=True, slots=True)
class Conversion:
    """Result of a UI -> API conversion, with the paper trail."""

    api: dict[str, Any]
    warnings: tuple[str, ...] = ()
    dropped: tuple[str, ...] = field(default=())

    def __len__(self) -> int:
        return len(self.api)


# --------------------------------------------------------------------------- shapes


def looks_like_ui_graph(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("nodes"), list) and "links" in data


def looks_like_api_prompt(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    return all(
        isinstance(node, dict) and "class_type" in node and "inputs" in node
        for node in data.values()
    )


# --------------------------------------------------------------------------- spec helpers


def _input_sections(spec: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Ordered `(name, options)` pairs declared by `/object_info` for one node class."""

    sections = spec.get("input") or {}
    order = spec.get("input_order") or {}
    names: list[tuple[str, str]] = []
    for section in ("required", "optional"):
        declared = sections.get(section) or {}
        ordered = order.get(section)
        if ordered:
            names.extend((name, section) for name in ordered if name in declared)
            names.extend((name, section) for name in declared if name not in ordered)
        else:
            names.extend((name, section) for name in declared)
    return [(name, sections[section][name]) for name, section in names]


def _is_widget(declared: Mapping[str, Any] | Any) -> bool:
    kind = declared[0] if isinstance(declared, (list, tuple)) and declared else declared
    if isinstance(kind, (list, tuple)):
        return True  # legacy combo: list of options
    return isinstance(kind, str) and kind in WIDGET_INPUT_TYPES


def _options(declared: Any) -> dict[str, Any]:
    if isinstance(declared, (list, tuple)) and len(declared) > 1 and isinstance(declared[1], dict):
        return dict(declared[1])
    return {}


def widget_names(spec: Mapping[str, Any]) -> list[str]:
    """Widget input names in declaration order (the order `widgets_values` follows)."""

    return [name for name, declared in _input_sections(spec) if _is_widget(declared)]


def _declared_for(spec: Mapping[str, Any], name: str) -> Any:
    for candidate, declared in _input_sections(spec):
        if candidate == name:
            return declared
    return None


def _kind_ok(declared: Any, value: Any) -> bool:
    """Type compatibility of one widget value, ignoring enum membership.

    A checkpoint name that merely is not installed yet is still a *string*: that is a
    MODEL_MISSING problem, not a misaligned widget. Only a value of the wrong kind
    (a float where an int belongs, an enum where text belongs) proves misalignment.
    """

    kind = declared[0] if isinstance(declared, (list, tuple)) and declared else declared
    if isinstance(kind, (list, tuple)) or kind in {"COMBO", "IMAGEUPLOAD", "AUDIOUPLOAD"}:
        return isinstance(value, (str, int, float, bool))
    if kind == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "FLOAT":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "STRING":
        return isinstance(value, str)
    if kind == "BOOLEAN":
        return isinstance(value, bool)
    return True


def _align_widgets(
    values: list[Any],
    slots: list[str],
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], list[int]]:
    """Assign positional widget values to named inputs, tolerating hidden widgets.

    The frontend appends values for widgets the API never receives:
    `control_after_generate` after a seed, a `dynamicPrompts` toggle after a text box,
    the upload button after `LoadImage.image`. Where they sit is frontend detail — and
    it moves between versions (`CLIPTextEncodeSDXL` parks *both* toggles at the end of
    the array) — so instead of guessing a fixed rule this searches the alignment that
    keeps every declared *type* satisfied, and prefers dropping values as late as
    possible, because hidden widgets trail the value they belong to.

    Ties are real: `LoadImage` carries `["example.png", "image"]`, where either value
    is a valid string. The late-skip cost breaks that tie towards `"example.png"`.
    """

    n_slots, n_values = len(slots), len(values)
    extras = n_values - n_slots
    if extras == 0:
        return dict(zip(slots, values, strict=False)), []

    kinds = [_declared_for(spec, name) for name in slots]

    def skip_cost(index: int) -> float:
        # dropping near the end is cheap; dropping early risks losing a real value
        return 1.0 + (n_values - index) / (n_values + 1)

    infeasible = float("inf")
    memo: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {}

    def best(slot: int, value: int) -> tuple[float, tuple[int, ...]]:
        key = (slot, value)
        cached = memo.get(key)
        if cached is not None:
            return cached
        remaining_slots = n_slots - slot
        remaining_values = n_values - value
        skips_left = extras - (value - slot)
        if remaining_slots == 0:
            result = (0.0, ()) if remaining_values == skips_left else (infeasible, ())
        elif remaining_values < remaining_slots or skips_left < 0:
            result = (infeasible, ())
        else:
            candidates: list[tuple[float, tuple[int, ...]]] = []
            if _kind_ok(kinds[slot], values[value]):
                sub_cost, sub_skips = best(slot + 1, value + 1)
                if sub_cost != infeasible:
                    candidates.append((sub_cost, sub_skips))
            if skips_left > 0:
                sub_cost, sub_skips = best(slot, value + 1)
                if sub_cost != infeasible:
                    candidates.append((skip_cost(value) + sub_cost, (value,) + sub_skips))
            result = min(candidates) if candidates else (infeasible, ())
        memo[key] = result
        return result

    cost, skipped = best(0, 0)
    if cost == infeasible:
        raise GraphError(
            "GRAPH_WIDGET_MISMATCH",
            f"{n_values} widget value(s) cannot be matched to the {n_slots} widget "
            f"input(s) {slots} declared by /object_info; widget order in this export "
            "does not match the node spec (common with custom packs) — export again "
            "from a frontend that matches the installed ComfyUI, or declare the "
            "workflow's widget names explicitly",
        )

    dropped = set(skipped)
    assignment: dict[str, Any] = {}
    cursor = 0
    for name in slots:
        while cursor in dropped:
            cursor += 1
        assignment[name] = values[cursor]
        cursor += 1
    return assignment, list(skipped)


# --------------------------------------------------------------------------- links


@dataclass(frozen=True, slots=True)
class _Resolved:
    kind: str  # "link" | "value"
    node_id: str | None = None
    slot: int | None = None
    value: Any = None


def _resolve_link(
    link_id: int,
    links: Mapping[int, Any],
    nodes: Mapping[str | int, Mapping[str, Any]],
    *,
    _depth: int = 0,
) -> _Resolved:
    if _depth > 64:
        raise GraphError("GRAPH_INVALID", f"link chain starting at {link_id} is cyclic")
    entry = links.get(link_id)
    if entry is None:
        raise GraphError("GRAPH_INVALID", f"link {link_id} is missing from the links table")

    # UI format: [link_id, from_node, from_slot, to_node, to_slot, type]
    if isinstance(entry, Mapping):
        from_node, from_slot = entry.get("origin_id"), entry.get("origin_slot")
    else:
        from_node, from_slot = entry[1], entry[2]

    source = nodes.get(from_node) or nodes.get(str(from_node))
    if source is None:
        raise GraphError("GRAPH_INVALID", f"link {link_id} points at missing node {from_node}")

    source_type = source.get("type", "")
    if source_type == "PrimitiveNode":
        values = source.get("widgets_values") or []
        if not values:
            raise GraphError(
                "GRAPH_INVALID",
                "PrimitiveNode has no value",
                node_id=from_node,
                node_type=source_type,
            )
        return _Resolved(kind="value", value=values[0])
    if source_type == "Reroute":
        incoming = source.get("inputs") or []
        if not incoming or incoming[0].get("link") is None:
            raise GraphError(
                "GRAPH_INVALID",
                "Reroute has no incoming link",
                node_id=from_node,
                node_type=source_type,
            )
        return _resolve_link(incoming[0]["link"], links, nodes, _depth=_depth + 1)
    if source.get("mode") == MODE_MUTED:
        raise GraphError(
            "GRAPH_INVALID",
            "connected node is muted; un-mute it or delete it before exporting",
            node_id=from_node,
            node_type=source_type,
        )
    return _Resolved(kind="link", node_id=str(from_node), slot=int(from_slot))


# --------------------------------------------------------------------------- conversion


def _consume_widget_values(
    node: Mapping[str, Any],
    spec: Mapping[str, Any],
    slots: list[str],
) -> tuple[dict[str, Any], list[str]]:
    node_id, node_type = node.get("id"), node.get("type")
    values = node.get("widgets_values")

    if isinstance(values, Mapping):
        # Newer frontends serialise widgets by name: nothing positional to guess.
        return {name: values[name] for name in slots if name in values}, []

    if values is None:
        values = []
    if not isinstance(values, list):
        raise GraphError(
            "GRAPH_INVALID",
            f"widgets_values must be a list or an object, got {type(values).__name__}",
            node_id=node_id,
            node_type=node_type,
        )

    if not slots:
        warning = []
        if values:
            warning.append(
                f"node {node_id} ({node_type}): {len(values)} widget value(s) but the "
                "/object_info spec declares no widget inputs; ignored"
            )
        return {}, warning

    if len(values) < len(slots):
        raise GraphError(
            "GRAPH_WIDGET_MISMATCH",
            f"spec declares {len(slots)} widget inputs {slots} but the export carries "
            f"only {len(values)} widget value(s)",
            node_id=node_id,
            node_type=node_type,
        )

    try:
        assignment, skipped = _align_widgets(values, slots, spec)
    except GraphError as exc:
        raise GraphError(exc.code, exc.message, node_id=node_id, node_type=node_type) from exc

    warnings: list[str] = []
    if skipped:
        positions = ", ".join(str(index) for index in skipped)
        warnings.append(
            f"node {node_id} ({node_type}): skipped widget value(s) at position "
            f"[{positions}] — hidden frontend widget with no API slot"
        )
    return assignment, warnings


def convert(graph: Mapping[str, Any], node_defs: Mapping[str, Any]) -> Conversion:
    """Turn a UI workflow into an API prompt.

    `node_defs` is the `/object_info` payload (or a captured fixture of it) used to
    resolve widget order and hidden widgets. Muted nodes are dropped, bypassed nodes are
    rejected: re-wiring a bypassed node correctly is the frontend's job, and silently
    producing a prompt that skips a node would change the result.
    """

    if looks_like_api_prompt(graph):
        return Conversion(api=dict(graph))

    nodes_raw = graph.get("nodes")
    if not isinstance(nodes_raw, list):
        raise GraphError("GRAPH_INVALID", "graph has no 'nodes' list")

    nodes: dict[str | int, Mapping[str, Any]] = {}
    for node in nodes_raw:
        if isinstance(node, Mapping) and "id" in node:
            nodes[node["id"]] = node

    links: dict[int, Any] = {}
    for entry in graph.get("links") or []:
        if isinstance(entry, Mapping):
            links[int(entry["id"])] = entry
        elif isinstance(entry, (list, tuple)) and entry:
            links[int(entry[0])] = entry

    api: dict[str, Any] = {}
    warnings: list[str] = []
    dropped: list[str] = []

    for node in nodes_raw:
        if not isinstance(node, Mapping):
            continue
        node_type = node.get("type")
        node_id = node.get("id")
        if node_type in VIRTUAL_NODE_TYPES:
            continue
        mode = node.get("mode", MODE_ALWAYS)
        if mode == MODE_MUTED:
            dropped.append(f"{node_id}:{node_type}")
            continue
        if mode == MODE_BYPASS:
            raise GraphError(
                "GRAPH_BYPASSED_NODE",
                "node is bypassed; the frontend would reconnect its upstream manually, "
                "which this converter does not emulate — un-bypass before exporting",
                node_id=node_id,
                node_type=node_type,
            )

        spec = node_defs.get(node_type)
        if spec is None:
            raise GraphError(
                "NODE_MISSING",
                "node class is not installed on the target ComfyUI instance",
                node_id=node_id,
                node_type=node_type,
            )

        ui_inputs = node.get("inputs") or []
        slots = [
            inp["name"]
            for inp in ui_inputs
            if isinstance(inp, Mapping) and isinstance(inp.get("widget"), Mapping)
        ]
        if not ui_inputs and not slots:
            slots = widget_names(spec)

        values, node_warnings = _consume_widget_values(node, spec, slots)
        warnings.extend(node_warnings)

        inputs: dict[str, Any] = {}
        for inp in ui_inputs:
            if not isinstance(inp, Mapping):
                continue
            name = inp.get("name")
            if not name:
                continue
            link_id = inp.get("link")
            if link_id is None:
                continue
            resolved = _resolve_link(link_id, links, nodes)
            inputs[name] = (
                resolved.value if resolved.kind == "value" else [resolved.node_id, resolved.slot]
            )

        for name, value in values.items():
            declared = _declared_for(spec, name)
            if declared is not None and not _kind_ok(declared, value):
                raise GraphError(
                    "GRAPH_WIDGET_MISMATCH",
                    f"widget {name!r} got {value!r}, but /object_info declares "
                    f"{declared[0] if isinstance(declared, (list, tuple)) else declared!r}; "
                    "widget order in this export does not match the node spec "
                    "(common with custom packs) — export again from a matching frontend",
                    node_id=node_id,
                    node_type=node_type,
                )
            inputs.setdefault(name, value)

        entry: dict[str, Any] = {"class_type": node_type, "inputs": inputs}
        title = node.get("title")
        if isinstance(title, str) and title:
            entry["_meta"] = {"title": title}
        api[str(node_id)] = entry

    if not api:
        raise GraphError("GRAPH_INVALID", "graph contains no executable nodes")
    return Conversion(api=api, warnings=tuple(warnings), dropped=tuple(dropped))


def ui_to_api(graph: Mapping[str, Any], node_defs: Mapping[str, Any]) -> dict[str, Any]:
    """`convert` for callers that only want the prompt."""

    return convert(graph, node_defs).api


# --------------------------------------------------------------------------- hashing


def strip_meta(api: Mapping[str, Any]) -> dict[str, Any]:
    """Drop cosmetic per-node metadata so titles do not change the hash."""

    return {
        str(node_id): {key: value for key, value in node.items() if key != "_meta"}
        for node_id, node in api.items()
        if isinstance(node, Mapping)
    }


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def prompt_hash(api: Mapping[str, Any]) -> str:
    """Stable SHA-256 of a prompt: independent of key order, node order and titles."""

    return hashlib.sha256(canonical_json(strip_meta(api)).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- validation


def _is_model_name(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(MODEL_SUFFIXES)


def _combo_options(declared: Any) -> list[Any] | None:
    if isinstance(declared, (list, tuple)) and declared:
        first = declared[0]
        if isinstance(first, (list, tuple)):
            return list(first)
        if isinstance(first, str) and first in {"COMBO", "COMBO_TYPE"}:
            options = _options(declared).get("options")
            return list(options) if isinstance(options, list) else None
    return None


def _type_ok(declared: Any, value: Any) -> bool:
    kind = declared[0] if isinstance(declared, (list, tuple)) and declared else declared
    if isinstance(kind, (list, tuple)):
        return not kind or value in kind
    if kind == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "FLOAT":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "STRING":
        return isinstance(value, str)
    if kind == "BOOLEAN":
        return isinstance(value, bool)
    return True


def validate_prompt(
    api: Mapping[str, Any],
    node_defs: Mapping[str, Any],
) -> list[Issue]:
    """Preflight a prompt against `/object_info`: the cheap failures we can catch
    before ComfyUI queues anything (missing node class, missing model file, dangling link)."""

    issues: list[Issue] = []
    seen_output = False

    for node_id, node in api.items():
        if not isinstance(node, Mapping):
            issues.append(Issue("GRAPH_INVALID", f"node {node_id} is not an object"))
            continue
        node_type = node.get("class_type", "")
        spec = node_defs.get(node_type)
        if spec is None:
            issues.append(
                Issue(
                    "NODE_MISSING",
                    f"node {node_id}: class {node_type!r} is not installed",
                    "install the custom node pack on the ComfyUI instance, or remove the node",
                    node_id=node_id,
                    node_type=node_type,
                )
            )
            continue
        if spec.get("output_node"):
            seen_output = True

        inputs = node.get("inputs") or {}
        for name, declared in _input_sections(spec):
            options = _options(declared)
            if name not in inputs:
                continue
            value = inputs[name]
            where = f"node {node_id}.{name}"
            if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str):
                target = api.get(value[0])
                if target is None:
                    issues.append(
                        Issue(
                            "LINK_MISSING",
                            f"{where} links to missing node {value[0]}",
                            "re-export the workflow; the link table references a deleted node",
                            node_id=node_id,
                            node_type=node_type,
                        )
                    )
                    continue
                outputs = (node_defs.get(target.get("class_type", "")) or {}).get("output") or []
                if isinstance(value[1], int) and value[1] >= len(outputs):
                    issues.append(
                        Issue(
                            "LINK_MISSING",
                            f"{where} links to output slot {value[1]} of "
                            f"{target.get('class_type')} which has {len(outputs)} output(s)",
                            "",
                            node_id=node_id,
                            node_type=node_type,
                        )
                    )
                continue
            options_list = _combo_options(declared)
            if options_list is not None and isinstance(value, str):
                if not options_list:
                    issues.append(
                        Issue(
                            "MODEL_MISSING",
                            f"{where}: no options available for {value!r}",
                            "the target ComfyUI instance has no files for this loader "
                            "(check its models directory)",
                            node_id=node_id,
                            node_type=node_type,
                        )
                    )
                    continue
                if value not in options_list:
                    if _is_model_name(value):
                        issues.append(
                            Issue(
                                "MODEL_MISSING",
                                f"{where}: {value!r} is not available",
                                "put the file in the right ComfyUI models directory, or override "
                                "this parameter (available: "
                                f"{', '.join(map(str, options_list[:5]))}...)",
                                node_id=node_id,
                                node_type=node_type,
                            )
                        )
                    else:
                        issues.append(
                            Issue(
                                "INVALID_PARAM",
                                f"{where}: {value!r} is not one of the declared options",
                                "",
                                node_id=node_id,
                                node_type=node_type,
                            )
                        )
                    continue
            if not _type_ok(declared, value):
                issues.append(
                    Issue(
                        "INVALID_PARAM",
                        f"{where}: expected {declared[0]!r}, got {type(value).__name__}",
                        "",
                        node_id=node_id,
                        node_type=node_type,
                    )
                )
                continue
            minimum, maximum = options.get("min"), options.get("max")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if minimum is not None and value < minimum:
                    issues.append(
                        Issue(
                            "INVALID_PARAM",
                            f"{where}: {value!r} is below the minimum {minimum!r}",
                            f"pass {name} >= {minimum}",
                            node_id=node_id,
                            node_type=node_type,
                        )
                    )
                elif maximum is not None and value > maximum:
                    issues.append(
                        Issue(
                            "INVALID_PARAM",
                            f"{where}: {value!r} is above the maximum {maximum!r}",
                            f"pass {name} <= {maximum}",
                            node_id=node_id,
                            node_type=node_type,
                        )
                    )

        sections = spec.get("input") or {}
        for name in sections.get("required") or {}:
            if name not in inputs:
                issues.append(
                    Issue(
                        "INPUT_MISSING",
                        f"node {node_id} ({node_type}) is missing required input {name!r}",
                        "the export may come from an older workflow; open and re-save it",
                        node_id=node_id,
                        node_type=node_type,
                    )
                )

    if not seen_output:
        issues.append(
            Issue(
                "OUTPUT_MISSING",
                "prompt has no output node (SaveImage / PreviewImage / ...), so nothing would run",
                "keep at least one output node in the workflow",
            )
        )
    return issues


def output_node_ids(api: Mapping[str, Any], node_defs: Mapping[str, Any]) -> list[str]:
    return [
        node_id
        for node_id, node in api.items()
        if (node_defs.get(node.get("class_type", "")) or {}).get("output_node")
    ]
