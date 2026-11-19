from __future__ import annotations

from pathlib import Path

import pytest

from comfy_mcp.config import ConfigError, Instance, load_settings


def test_defaults_without_any_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings(env={})

    assert [i.name for i in settings.instances] == ["default"]
    assert settings.instance().url == "http://127.0.0.1:8188"
    assert settings.default_timeout_s == 300
    assert settings.preview_max_edge == 512
    assert settings.transport == "stdio"


def test_env_overrides(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "COMFY_MCP_INSTANCES": str(tmp_path / "missing.yaml"),
            "COMFYUI_URL": "http://192.168.1.9:8190",
            "COMFY_MCP_DEFAULT_TIMEOUT_S": "42",
            "COMFY_MCP_PREVIEW_MAX_EDGE": "256",
            "COMFY_MCP_TRANSPORT": "http",
            "COMFY_MCP_HTTP_PORT": "9000",
            "COMFY_MCP_WORKFLOWS": "wf",
            "COMFY_MCP_RUNS": "out",
        }
    )
    assert settings.instance().url == "http://192.168.1.9:8190"
    assert settings.default_timeout_s == 42
    assert settings.preview_max_edge == 256
    assert settings.transport == "http"
    assert settings.http_port == 9000
    assert settings.workflows_dir == Path("wf")
    assert settings.runs_dir == Path("out")


def test_instances_yaml(tmp_path: Path) -> None:
    path = tmp_path / "instances.yaml"
    path.write_text(
        "instances:\n"
        "  - name: local\n"
        "    url: http://127.0.0.1:8188/\n"
        "    tags: [gpu8g]\n"
        "    models: [sd15.safetensors]\n"
        "  - name: remote\n"
        "    url: https://gpu.example.com\n",
        encoding="utf-8",
    )
    settings = load_settings(env={"COMFY_MCP_INSTANCES": str(path)})

    assert [i.name for i in settings.instances] == ["local", "remote"]
    assert settings.instance().http_base == "http://127.0.0.1:8188"
    assert settings.instance("remote").ws_url == "wss://gpu.example.com/ws"
    assert settings.instance("local").tags == ("gpu8g",)


def test_ws_url_mapping() -> None:
    assert Instance(name="a", url="http://127.0.0.1:8188").ws_url == "ws://127.0.0.1:8188/ws"
    assert Instance(name="b", url="https://x.test/mcp/").ws_url == "wss://x.test/mcp/ws"


@pytest.mark.parametrize(
    "env,message",
    [
        ({"COMFY_MCP_DEFAULT_TIMEOUT_S": "abc"}, "must be an integer"),
        ({"COMFY_MCP_DEFAULT_TIMEOUT_S": "0"}, "must be >= 1"),
        ({"COMFY_MCP_TRANSPORT": "carrier-pigeon"}, "stdio"),
    ],
)
def test_bad_env(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings(env={**env, "COMFY_MCP_INSTANCES": "nope.yaml"})


def test_unknown_instance_name() -> None:
    settings = load_settings(env={"COMFY_MCP_INSTANCES": "nope.yaml"})
    with pytest.raises(ConfigError, match="unknown instance"):
        settings.instance("gpu-9000")


def test_duplicate_instance_names(tmp_path: Path) -> None:
    path = tmp_path / "instances.yaml"
    path.write_text(
        "instances:\n"
        "  - {name: a, url: 'http://127.0.0.1:8188'}\n"
        "  - {name: a, url: 'http://127.0.0.1:8189'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_settings(env={"COMFY_MCP_INSTANCES": str(path)})
