"""Model weight digests, cached by (size, mtime_ns).

ComfyUI exposes model filenames but no checksums, so the only honest digest is one
computed from the file on disk. That costs two things: the MCP server must be able to
see ComfyUI's models directory (COMFY_MCP_MODELS_DIR), and it must never re-read 6 GB
because you asked twice -- hence the cache keyed on size + mtime.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

CHUNK = 1 << 20
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")

log = logging.getLogger(__name__)


class ModelHasher:
    """sha256 of model files, with a filename index and an on-disk cache."""

    def __init__(self, models_dir: Path | None, cache_path: Path | None = None) -> None:
        self.models_dir = Path(models_dir) if models_dir else None
        self.cache_path = Path(cache_path) if cache_path else None
        self._index: dict[str, Path] | None = None
        self._cache: dict[str, dict[str, Any]] = self._read_cache()
        self.hits = 0
        self.misses = 0

    @property
    def available(self) -> bool:
        return self.models_dir is not None and self.models_dir.is_dir()

    def find(self, name: str) -> Path | None:
        """Locate a model by basename anywhere under models/ (checkpoints/, loras/, ...)."""
        if self._index is None:
            self._index = self._build_index()
        return self._index.get(Path(name).name)

    def _build_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        if not self.available:
            return index
        assert self.models_dir is not None
        for path in sorted(self.models_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MODEL_SUFFIXES:
                continue
            if path.name in index:
                log.debug("duplicate model name %s, keeping %s", path.name, index[path.name])
                continue
            index[path.name] = path
        return index

    def sha256(self, name: str) -> str | None:
        """Digest one model, or None when it is not reachable from this process."""
        if not self.available:
            return None
        path = self.find(name)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError as exc:
            log.warning("cannot stat model %s: %s", path, exc)
            return None
        key = str(path.resolve())
        cached = self._cache.get(key)
        if (
            cached
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
        ):
            self.hits += 1
            return str(cached.get("sha256") or "") or None
        self.misses += 1
        digest = digest_file(path)
        self._cache[key] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
        }
        self._write_cache()
        return digest

    def _read_cache(self) -> dict[str, dict[str, Any]]:
        if self.cache_path is None or not self.cache_path.exists():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable checksum cache %s: %s", self.cache_path, exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_cache(self) -> None:
        if self.cache_path is None:
            return
        with suppress(OSError):
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self.cache_path.parent, prefix=".checksums-", suffix=".tmp"
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(self._cache, handle, indent=1, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.cache_path)
            except OSError:
                with suppress(OSError):
                    tmp.unlink()


def digest_file(path: Path) -> str:
    """Streaming sha256: 1 MiB chunks so a 6 GB checkpoint does not need 6 GB of RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
