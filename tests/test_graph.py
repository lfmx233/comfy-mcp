"""graph: UI export -> API prompt, hashing, preflight.

The fixtures are real exports from the machine this project was built against
(ComfyUI 0.3.60, RTX 4060 8G) plus its /object_info snapshot, so a regression
here means a real workflow stopped converting.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from comfy_mcp.graph import (
    GraphError,
    canonical_json,
    convert,
    looks_like_api_prompt,
    looks_like_ui_graph,
    output_node_ids,
    prompt_hash,
    strip_meta,
    ui_to_api,
    validate_prompt,
    widget_names,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def node_defs() -> dict:
    return json.loads((FIXTURES / "object_info_0.3.60.json").read_text(encoding="utf-8"))


def load(name: str) -> dict:
    return json.loads((FIXTURES / "workflows" / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- conversion


def test_txt2img_converts_seven_widget_values_and_links(node_defs):
    conversion = convert(load("txt2img_ui"), node_defs)

    assert len(conversion) == 8
    sampler = conversion.api["6"]["inputs"]
    # the export carries [seed, "randomize", steps, cfg, sampler, scheduler, denoise]:
    # "randomize" is the hidden control_after_generate widget and must be dropped
    assert sampler["seed"] == 897283906030423
    assert sampler["steps"] == 40
    assert sampler["cfg"] == 4
    assert sampler["sampler_name"] == "dpmpp_2m"
    assert sampler["scheduler"] == "karras"
    assert sampler["denoise"] == 1
    assert sampler["positive"] == ["5", 0]
    assert sampler["negative"] == ["4", 0]
    assert sampler["model"] == ["2", 0]
    assert conversion.api["8"]["inputs"]["vae"] == ["1", 2]


def test_hidden_widget_skip_is_reported_not_silent(node_defs):
    conversion = convert(load("txt2img_ui"), node_defs)
    assert any("skipped widget value" in warning for warning in conversion.warnings)


def test_sdxl_toggles_at_end_of_array_still_align(node_defs):
    """CLIPTextEncodeSDXL parks both dynamicPrompts toggles after all real widgets."""

    conversion = convert(load("sdxl_txt2img_ui"), node_defs)
    inputs = conversion.api["3"]["inputs"]

    assert inputs["text_g"] == ""
    assert inputs["text_l"].startswith("Girl, pink hair")
    assert inputs["width"] == 4096
    assert inputs["target_height"] == 4096


def test_primitive_node_becomes_a_literal_value(node_defs):
    """text_l is driven by a PrimitiveNode in the export; the API needs the text itself."""

    conversion = convert(load("sdxl_txt2img_ui"), node_defs)
    assert isinstance(conversion.api["3"]["inputs"]["text_l"], str)
    assert "PrimitiveNode" not in {node["class_type"] for node in conversion.api.values()}


def test_load_image_upload_widget_does_not_steal_the_filename(node_defs):
    conversion = convert(load("img2img_ui"), node_defs)
    assert conversion.api["1"]["inputs"]["image"].endswith(".png")


def test_custom_node_upscale_aligns_by_type(node_defs):
    """UltimateSDUpscale is a custom node; its widget order must not silently shift."""

    conversion = convert(load("upscale_ui"), node_defs)
    inputs = conversion.api["31"]["inputs"]
    assert inputs["upscale_by"] == 2
    assert isinstance(inputs["steps"], int)
    assert isinstance(inputs["denoise"], float)


def test_reroute_hops_are_resolved_to_the_real_source(node_defs):
    conversion = convert(load("upscale_ui"), node_defs)
    for node in conversion.api.values():
        assert node["class_type"] != "Reroute"
    # node 25 sits behind a Reroute; it must point at the real producer, not the hop
    assert conversion.api["25"]["inputs"]["image"][0] in conversion.api


def test_muted_node_is_dropped_and_bypassed_node_is_rejected(node_defs):
    graph = load("txt2img_ui")
    api = ui_to_api(graph, node_defs)
    assert "9" in api

    muted = copy.deepcopy(graph)
    next(n for n in muted["nodes"] if n["id"] == 9)["mode"] = 2
    conversion = convert(muted, node_defs)
    assert "9" not in conversion.api
    assert "9:PreviewImage" in conversion.dropped

    bypassed = copy.deepcopy(graph)
    next(n for n in bypassed["nodes"] if n["id"] == 2)["mode"] = 4
    with pytest.raises(GraphError) as excinfo:
        convert(bypassed, node_defs)
    assert excinfo.value.code == "GRAPH_BYPASSED_NODE"


def test_unknown_node_class_fails_with_node_missing(node_defs):
    graph = load("txt2img_ui")
    graph["nodes"][0]["type"] = "TotallyCustomNode"
    with pytest.raises(GraphError) as excinfo:
        convert(graph, node_defs)
    assert excinfo.value.code == "NODE_MISSING"
    assert excinfo.value.node_type == "TotallyCustomNode"


def test_widget_alignment_failure_is_loud(node_defs):
    """Two ints cannot fill a single STRING slot: refuse rather than shift values."""

    graph = load("txt2img_ui")
    node = next(n for n in graph["nodes"] if n["type"] == "CLIPTextEncode")
    node["widgets_values"] = [123, 456]
    with pytest.raises(GraphError) as excinfo:
        convert(graph, node_defs)
    assert excinfo.value.code == "GRAPH_WIDGET_MISMATCH"
    assert excinfo.value.node_id == str(node["id"])


def test_api_prompt_passes_through_untouched(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)
    assert convert(api, node_defs).api == api


def test_shapes_are_distinguishable(node_defs):
    assert looks_like_ui_graph(load("txt2img_ui"))
    assert not looks_like_api_prompt(load("txt2img_ui"))
    assert looks_like_api_prompt(ui_to_api(load("txt2img_ui"), node_defs))
    assert not looks_like_ui_graph(ui_to_api(load("txt2img_ui"), node_defs))


# --------------------------------------------------------------------------- hashing


def test_hash_is_stable_across_node_order_and_titles(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)

    shuffled = dict(reversed(list(api.items())))
    shuffled["6"] = dict(shuffled["6"], _meta={"title": "renamed sampler"})

    assert prompt_hash(shuffled) == prompt_hash(api)
    assert canonical_json(strip_meta(shuffled)) == canonical_json(strip_meta(api))


def test_hash_changes_when_a_value_changes(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)
    other = copy.deepcopy(api)
    other["6"]["inputs"]["seed"] = 1
    assert prompt_hash(other) != prompt_hash(api)


def test_repeated_conversion_produces_the_same_hash(node_defs):
    assert prompt_hash(convert(load("sdxl_txt2img_ui"), node_defs).api) == prompt_hash(
        convert(load("sdxl_txt2img_ui"), node_defs).api
    )


# --------------------------------------------------------------------------- validation


def test_validate_reports_missing_model(node_defs):
    issues = validate_prompt(ui_to_api(load("txt2img_ui"), node_defs), node_defs)
    missing = [issue for issue in issues if issue.code == "MODEL_MISSING"]
    assert missing and missing[0].node_id == "1"
    assert "cuteyukimix" in missing[0].message


def test_validate_accepts_sdxl_workflow_unchanged(node_defs):
    assert validate_prompt(ui_to_api(load("sdxl_txt2img_ui"), node_defs), node_defs) == []


def test_validate_flags_out_of_range_and_missing_inputs(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)
    api["1"]["inputs"]["ckpt_name"] = "anything-v5-PrtRE.safetensors"
    api["6"]["inputs"]["steps"] = 0
    del api["6"]["inputs"]["cfg"]

    codes = {issue.code for issue in validate_prompt(api, node_defs)}
    assert "INVALID_PARAM" in codes
    assert "INPUT_MISSING" in codes


def test_validate_flags_dangling_links(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)
    api["1"]["inputs"]["ckpt_name"] = "anything-v5-PrtRE.safetensors"
    api["6"]["inputs"]["model"] = ["999", 0]
    issues = validate_prompt(api, node_defs)
    assert [issue.code for issue in issues] == ["LINK_MISSING"]


def test_validate_requires_an_output_node(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)
    api["1"]["inputs"]["ckpt_name"] = "anything-v5-PrtRE.safetensors"
    del api["9"]
    issues = validate_prompt(api, node_defs)
    assert [issue.code for issue in issues] == ["OUTPUT_MISSING"]


def test_output_node_ids_uses_the_object_info_flag(node_defs):
    api = ui_to_api(load("txt2img_ui"), node_defs)
    assert output_node_ids(api, node_defs) == ["9"]


def test_widget_names_follows_declaration_order(node_defs):
    assert widget_names(node_defs["KSampler"])[:3] == ["seed", "steps", "cfg"]
