"""registry: sidecar-declared parameters over pinned graphs.

Two things are load-bearing here and both are tested against the real workflows that ship
in `workflows/`: parameters may only touch declared paths, and the graph hash must not
move when a parameter changes.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from comfy_mcp.graph import prompt_hash
from comfy_mcp.registry import (
    Registry,
    RegistryError,
    WorkflowSpec,
    apply_params,
    load_registry,
    load_workflow,
    resolve_params,
    validate_workflow,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def node_defs() -> dict:
    return json.loads((FIXTURES / "object_info_0.3.60.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry(node_defs) -> Registry:
    return load_registry(WORKFLOWS, node_defs)


@pytest.fixture(scope="module")
def txt2img(registry) -> WorkflowSpec:
    return registry.get("txt2img_anime")


def write_sidecar(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- loading


def test_all_three_workflows_load(registry):
    assert registry.names() == ["img2img", "sdxl_txt2img", "txt2img_anime"]
    for spec in registry.list():
        assert spec.graph_hash.startswith("sha256:")
        assert spec.params
        assert spec.description


def test_info_matches_the_documented_contract(registry):
    info = registry.get("txt2img_anime").info()
    assert set(info) == {"name", "description", "tags", "graph_hash", "params"}
    assert info["params"]["prompt"]["required"] is True
    assert info["params"]["width"]["enum"] == [512, 640, 768, 896, 1024]
    assert info["params"]["seed"]["default"] == -1


def test_hash_is_the_hash_of_the_pinned_graph(txt2img):
    assert txt2img.graph_hash == f"sha256:{prompt_hash(txt2img.api)}"


def test_hash_survives_a_reload(node_defs, registry, txt2img):
    again = load_registry(WORKFLOWS, node_defs)
    assert again.get("txt2img_anime").graph_hash == txt2img.graph_hash


def test_real_workflows_pass_preflight(registry, node_defs):
    for spec in registry.list():
        assert validate_workflow(spec, node_defs) == [], spec.name


def test_tag_filter(registry):
    assert [spec.name for spec in registry.list("sdxl")] == ["sdxl_txt2img"]
    assert [spec.name for spec in registry.list("audio")] == []


def test_unknown_workflow_names_the_known_ones(registry):
    with pytest.raises(RegistryError) as excinfo:
        registry.get("txt2video")
    assert excinfo.value.code == "UNKNOWN_WORKFLOW"
    assert "txt2img_anime" in str(excinfo.value)


# --------------------------------------------------------------------------- params


def test_defaults_are_applied_and_seed_minus_one_is_resolved(txt2img):
    resolved = resolve_params(txt2img, {"prompt": "1girl"}, rng=random.Random(7))
    assert resolved["prompt"] == "1girl"
    assert resolved["steps"] == 30
    assert 0 <= resolved["seed"] <= 4294967295
    assert resolved["seed"] != -1


def test_explicit_seed_is_kept(txt2img):
    resolved = resolve_params(txt2img, {"prompt": "x", "seed": 12345}, rng=random.Random(1))
    assert resolved["seed"] == 12345


def test_missing_required_param_is_rejected(txt2img):
    with pytest.raises(RegistryError) as excinfo:
        resolve_params(txt2img, {})
    assert excinfo.value.code == "INVALID_PARAM"
    assert "prompt" in str(excinfo.value)


def test_unknown_param_lists_what_is_declared(txt2img):
    with pytest.raises(RegistryError) as excinfo:
        resolve_params(txt2img, {"prompt": "x", "num_steps": 20})
    assert excinfo.value.code == "INVALID_PARAM"
    assert "steps" in str(excinfo.value)


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"prompt": "x", "steps": 0}, ">="),
        ({"prompt": "x", "steps": 500}, "<="),
        ({"prompt": "x", "steps": "30"}, "integer"),
        ({"prompt": "x", "width": 700}, "one of"),
        ({"prompt": 42}, "string"),
        ({"prompt": "x", "cfg": True}, "number"),
    ],
)
def test_param_validation_messages(txt2img, params, needle):
    with pytest.raises(RegistryError) as excinfo:
        resolve_params(txt2img, params)
    assert excinfo.value.code == "INVALID_PARAM"
    assert needle in str(excinfo.value)


def test_prompt_over_max_len_is_rejected(txt2img):
    with pytest.raises(RegistryError) as excinfo:
        resolve_params(txt2img, {"prompt": "x" * 801})
    assert excinfo.value.code == "INVALID_PARAM"
    assert "limit" in str(excinfo.value)


# --------------------------------------------------------------------------- injection


def test_apply_writes_only_the_declared_paths(txt2img):
    resolved = resolve_params(txt2img, {"prompt": "rainy street", "seed": 1}, rng=random.Random(0))
    api = apply_params(txt2img, resolved)

    assert api["5"]["inputs"]["text"] == "rainy street"
    assert api["6"]["inputs"]["seed"] == 1
    assert api["6"]["inputs"]["steps"] == 30
    assert api["7"]["inputs"]["width"] == 512
    assert api["2"]["inputs"]["strength_model"] == 0.8


def test_apply_does_not_mutate_the_registry(txt2img):
    before = json.dumps(txt2img.api, sort_keys=True)
    apply_params(txt2img, resolve_params(txt2img, {"prompt": "mutate me"}, rng=random.Random(0)))
    assert json.dumps(txt2img.api, sort_keys=True) == before


def test_prompt_change_does_not_move_the_graph_hash(txt2img):
    api = apply_params(txt2img, resolve_params(txt2img, {"prompt": "different"}))
    assert prompt_hash(api) != txt2img.graph_hash.removeprefix("sha256:")


def test_fan_out_param_writes_every_path(registry):
    sdxl = registry.get("sdxl_txt2img")
    api = apply_params(sdxl, resolve_params(sdxl, {"prompt": "1girl", "steps": 12, "width": 896}))

    assert api["15"]["inputs"]["steps"] == 12
    assert api["16"]["inputs"]["steps"] == 12
    assert api["3"]["inputs"]["text_g"] == api["3"]["inputs"]["text_l"] == "1girl"
    assert api["10"]["inputs"]["text"] == "1girl"
    for node_id in ("6", "3", "4", "10", "12"):
        assert api[node_id]["inputs"]["width"] == 896
    assert api["3"]["inputs"]["target_width"] == 896


def test_img2img_denoise_is_injectable(registry):
    spec = registry.get("img2img")
    api = apply_params(spec, resolve_params(spec, {"prompt": "x", "denoise": 0.3}))
    assert api["12"]["inputs"]["denoise"] == 0.3


# --------------------------------------------------------------------------- sidecar errors


def test_sidecar_name_must_match_its_filename(node_defs, tmp_path):
    (tmp_path / "txt2img_anime.json").write_text(
        (WORKFLOWS / "txt2img_anime.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar = write_sidecar(
        tmp_path,
        "txt2img_anime",
        "name: other\ndescription: d\nparams: {}\n",
    )
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "must match the file stem" in str(excinfo.value)


def test_sidecar_unknown_key_is_rejected(node_defs, tmp_path):
    (tmp_path / "wf.json").write_text(
        (WORKFLOWS / "sdxl_txt2img.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar = write_sidecar(tmp_path, "wf", "name: wf\ndescription: d\nsteps_typo: 1\n")
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "unknown key" in str(excinfo.value)


def test_param_path_must_exist_in_the_graph(node_defs, tmp_path):
    (tmp_path / "wf.json").write_text(
        (WORKFLOWS / "sdxl_txt2img.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar = write_sidecar(
        tmp_path,
        "wf",
        "name: wf\ndescription: d\nparams:\n  prompt:\n    type: string\n"
        "    path: '99.inputs.text'\n",
    )
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "not in the graph" in str(excinfo.value)


def test_param_path_type_must_match_the_param_type(node_defs, tmp_path):
    (tmp_path / "wf.json").write_text(
        (WORKFLOWS / "sdxl_txt2img.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar = write_sidecar(
        tmp_path,
        "wf",
        "name: wf\ndescription: d\nparams:\n  prompt:\n    type: integer\n"
        "    path: '3.inputs.text_l'\n",
    )
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "points at" in str(excinfo.value)


def test_param_needs_exactly_one_of_path_and_paths(node_defs, tmp_path):
    (tmp_path / "wf.json").write_text(
        (WORKFLOWS / "sdxl_txt2img.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar = write_sidecar(
        tmp_path,
        "wf",
        "name: wf\ndescription: d\nparams:\n  prompt:\n    type: string\n",
    )
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "path" in str(excinfo.value)


def test_duplicate_names_across_sidecars_are_rejected(node_defs, tmp_path):
    (tmp_path / "wf.json").write_text(
        (WORKFLOWS / "sdxl_txt2img.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    body = "name: wf\ndescription: d\nparams: {}\n"
    write_sidecar(tmp_path, "wf", body)
    (tmp_path / "wf.yml").write_text(body, encoding="utf-8")
    with pytest.raises(RegistryError) as excinfo:
        load_registry(tmp_path, node_defs)
    assert "duplicate workflow name" in str(excinfo.value)


def test_missing_workflow_file_is_reported(node_defs, tmp_path):
    sidecar = write_sidecar(tmp_path, "wf", "name: wf\ndescription: d\nparams: {}\n")
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "not found" in str(excinfo.value)


def test_missing_description_is_rejected(node_defs, tmp_path):
    (tmp_path / "wf.json").write_text(
        (WORKFLOWS / "sdxl_txt2img.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sidecar = write_sidecar(tmp_path, "wf", "name: wf\nparams: {}\n")
    with pytest.raises(RegistryError) as excinfo:
        load_workflow(sidecar, node_defs)
    assert "description is required" in str(excinfo.value)


def test_empty_directory_is_an_empty_registry(tmp_path, node_defs):
    assert len(load_registry(tmp_path, node_defs)) == 0
