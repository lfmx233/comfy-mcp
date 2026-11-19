"""Model digests: computed once, cached by size + mtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from comfy_mcp.hashes import ModelHasher, digest_file


def test_digest_file_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"comfy-mcp" * 1000)
    assert digest_file(path) == hashlib.sha256(b"comfy-mcp" * 1000).hexdigest()


def test_digest_handles_files_larger_than_one_chunk(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (3 * 1024 * 1024 + 7))
    assert digest_file(path) == hashlib.sha256(b"x" * (3 * 1024 * 1024 + 7)).hexdigest()


def test_hasher_finds_models_in_subdirectories(tmp_path: Path) -> None:
    models = tmp_path / "models"
    nested = models / "checkpoints" / "sd"
    nested.mkdir(parents=True)
    (nested / "sd15.safetensors").write_bytes(b"weights")

    hasher = ModelHasher(models)
    assert hasher.available is True
    found = hasher.find("sd15.safetensors")
    assert found is not None
    assert found.name == "sd15.safetensors"
    assert hasher.find("absent.safetensors") is None


def test_second_digest_is_a_cache_hit(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "sd15.safetensors").write_bytes(b"weights")
    cache = tmp_path / "runs" / "checksums.json"

    hasher = ModelHasher(models, cache)
    first = hasher.sha256("sd15.safetensors")
    second = hasher.sha256("sd15.safetensors")

    assert first == hashlib.sha256(b"weights").hexdigest()
    assert first == second
    assert hasher.hits == 1
    stored = json.loads(cache.read_text(encoding="utf-8"))
    entry = stored[str((models / "sd15.safetensors").resolve())]
    assert entry["sha256"] == first
    assert entry["size"] == len(b"weights")


def test_cache_is_invalidated_when_the_file_changes(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    path = models / "sd15.safetensors"
    path.write_bytes(b"weights")
    hasher = ModelHasher(models, tmp_path / "checksums.json")

    before = hasher.sha256("sd15.safetensors")
    path.write_bytes(b"other weights")
    after = hasher.sha256("sd15.safetensors")

    assert before != after
    assert after == hashlib.sha256(b"other weights").hexdigest()
    assert hasher.hits == 0


def test_missing_models_dir_degrades_to_none(tmp_path: Path) -> None:
    hasher = ModelHasher(tmp_path / "no-models")
    assert hasher.available is False
    assert hasher.sha256("sd15.safetensors") is None
    assert hasher.hits == 0


def test_unknown_model_is_none_not_an_error(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "sd15.safetensors").write_bytes(b"weights")
    assert ModelHasher(models, tmp_path / "checksums.json").sha256("other.ckpt") is None


def test_corrupt_cache_is_ignored(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "sd15.safetensors").write_bytes(b"weights")
    cache = tmp_path / "checksums.json"
    cache.write_text("{not json", encoding="utf-8")

    hasher = ModelHasher(models, cache)
    digest = hasher.sha256("sd15.safetensors")
    assert digest == hashlib.sha256(b"weights").hexdigest()
    assert hasher.hits == 0
