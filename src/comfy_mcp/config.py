"""Configuration for comfy-mcp.

Everything the server needs before it touches the network: which ComfyUI
instances exist, where workflows live, and the tunables that control
timeouts and how much image data we are willing to put in a context window.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_INSTANCE_URL = "http://127.0.0.1:8188"


class ConfigError(RuntimeError):
    """Configuration is missing, malformed, or points at something unknown."""


@dataclass(frozen=True, slots=True)
class Instance:
    """One reachable ComfyUI server."""

    name: str
    url: str
    tags: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("instance name must not be empty")
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(
                f"instance {self.name!r}: url must start with http(s)://, got {self.url!r}"
            )

    @property
    def http_base(self) -> str:
        return self.url.rstrip("/")

    @property
    def ws_url(self) -> str:
        """Websocket endpoint; the client appends ?clientId=..."""
        base = self.http_base
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/ws"
        return base + "/ws"


@dataclass(frozen=True, slots=True)
class Settings:
    instances: tuple[Instance, ...]
    workflows_dir: Path
    runs_dir: Path
    default_timeout_s: int = 300
    preview_max_edge: int = 512
    transport: str = "stdio"
    http_port: int = 8848
    max_upload_mb: int = 20
    max_queue: int = 20

    def instance(self, name: str | None = None) -> Instance:
        """Return the named instance, or the first one when no name is given."""
        if not self.instances:
            raise ConfigError("no ComfyUI instances configured")
        if name is None:
            return self.instances[0]
        for inst in self.instances:
            if inst.name == name:
                return inst
        known = ", ".join(i.name for i in self.instances)
        raise ConfigError(f"unknown instance {name!r}; known instances: {known}")


def _int(env: Mapping[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _load_instances(path: Path, env: Mapping[str, str]) -> tuple[Instance, ...]:
    """Load instances.yaml, falling back to a single local instance.

    A missing file is not an error: the common case is one ComfyUI on
    127.0.0.1:8188 and nothing to configure.
    """
    if not path.exists():
        url = env.get("COMFYUI_URL", DEFAULT_INSTANCE_URL)
        return (Instance(name="default", url=url),)

    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "instances" not in raw:
        raise ConfigError(f"{path}: expected a mapping with an 'instances' key")

    items = raw["instances"]
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path}: 'instances' must be a non-empty list")

    instances: list[Instance] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: instances[{index}] must be a mapping")
        try:
            name = str(item["name"])
            url = str(item["url"])
        except KeyError as exc:
            raise ConfigError(f"{path}: instances[{index}] is missing {exc.args[0]!r}") from exc
        instances.append(
            Instance(
                name=name,
                url=url,
                tags=tuple(str(t) for t in item.get("tags") or ()),
                models=tuple(str(m) for m in item.get("models") or ()),
            )
        )

    names = [i.name for i in instances]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"{path}: duplicate instance names: {sorted(duplicates)}")
    return tuple(instances)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from environment variables (and instances.yaml)."""
    env = dict(os.environ if env is None else env)

    transport = env.get("COMFY_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in {"stdio", "http"}:
        raise ConfigError(f"COMFY_MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}")

    return Settings(
        instances=_load_instances(Path(env.get("COMFY_MCP_INSTANCES", "instances.yaml")), env),
        workflows_dir=Path(env.get("COMFY_MCP_WORKFLOWS", "workflows")),
        runs_dir=Path(env.get("COMFY_MCP_RUNS", "runs")),
        default_timeout_s=_int(env, "COMFY_MCP_DEFAULT_TIMEOUT_S", 300, minimum=1),
        preview_max_edge=_int(env, "COMFY_MCP_PREVIEW_MAX_EDGE", 512, minimum=64),
        transport=transport,
        http_port=_int(env, "COMFY_MCP_HTTP_PORT", 8848, minimum=1),
        max_upload_mb=_int(env, "COMFY_MCP_MAX_UPLOAD_MB", 20, minimum=1),
        max_queue=_int(env, "COMFY_MCP_MAX_QUEUE", 20, minimum=1),
    )
