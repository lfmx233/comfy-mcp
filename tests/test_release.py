"""Release plumbing: the registry manifest, the version, and the package name agree."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from comfy_mcp import __version__

REPO = Path(__file__).resolve().parents[1]


def load_json(name: str) -> dict:
    return json.loads((REPO / name).read_text(encoding="utf-8"))


def test_the_pyproject_and_the_package_tell_the_same_version() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__


def test_the_registry_manifest_matches_the_package() -> None:
    manifest = load_json("server.json")
    package = manifest["packages"][0]
    assert manifest["name"] == "io.github.lfmx233/comfy-mcp"
    assert manifest["version"] == __version__
    assert package["identifier"] == "comfy-mcp"
    assert package["version"] == __version__
    assert package["registryType"] == "pypi"
    assert package["transport"] == {"type": "stdio"}


def test_the_manifest_points_at_this_repository() -> None:
    manifest = load_json("server.json")
    assert manifest["repository"] == {
        "url": "https://github.com/lfmx233/comfy-mcp",
        "source": "github",
    }


def test_every_environment_variable_it_advertises_is_one_the_code_reads() -> None:
    """A variable in the manifest that config.py never reads is a lie to the user."""
    manifest = load_json("server.json")
    config = (REPO / "src" / "comfy_mcp" / "config.py").read_text(encoding="utf-8")
    declared = [item["name"] for item in manifest["packages"][0]["environmentVariables"]]
    assert declared, "the manifest should advertise at least one variable"
    for name in declared:
        assert name in config, f"{name} is advertised but config.py never reads it"


def test_the_manifest_stays_under_the_registry_length_limit() -> None:
    manifest = load_json("server.json")
    assert 1 <= len(manifest["description"]) <= 100
