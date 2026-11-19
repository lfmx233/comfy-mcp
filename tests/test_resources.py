"""Image plumbing: sizes without Pillow, previews with it, links instead of base64."""

from __future__ import annotations

import base64
import io

import pytest

from comfy_mcp.resources import (
    INLINE_LIMIT,
    image_block,
    image_mime,
    image_size,
    preview,
    resource_link,
)
from tests.fake_comfy.app import PNG_1X1


def _png(width: int, height: int, *, alpha: bool = False) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    mode = "RGBA" if alpha else "RGB"
    Image.new(mode, (width, height), (10, 20, 30, 255) if alpha else (10, 20, 30)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 100, 50)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_mime_and_size_of_a_png() -> None:
    assert image_mime(PNG_1X1) == "image/png"
    assert image_size(PNG_1X1) == (1, 1)


def test_size_of_a_jpeg_without_pillow() -> None:
    assert image_size(_jpeg(320, 200)) == (320, 200)


def test_size_of_a_large_png() -> None:
    assert image_size(_png(1024, 768)) == (1024, 768)


def test_garbage_is_not_an_image() -> None:
    assert image_mime(b"not an image at all") is None
    assert image_size(b"not an image at all") is None


def test_empty_bytes_do_not_raise() -> None:
    assert image_size(b"") is None
    assert image_mime(b"") is None


def test_small_images_are_sent_untouched() -> None:
    payload, mime = preview(PNG_1X1, max_edge=512)
    assert payload == PNG_1X1
    assert mime == "image/png"


def test_long_edge_is_scaled_down_to_the_limit() -> None:
    original = _png(1024, 512)
    payload, mime = preview(original, max_edge=512)

    size = image_size(payload)
    assert size is not None
    assert max(size) == 512
    assert size == (512, 256)
    assert mime == "image/jpeg"
    assert len(payload) < len(original)


def test_transparent_images_stay_png() -> None:
    payload, mime = preview(_png(1024, 1024, alpha=True), max_edge=256)
    assert mime == "image/png"
    assert image_mime(payload) == "image/png"


def test_a_heavy_but_small_image_is_re_encoded() -> None:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.effect_noise((512, 512), 64).save(buffer, format="PNG")
    heavy = buffer.getvalue()
    assert len(heavy) > INLINE_LIMIT

    payload, mime = preview(heavy, max_edge=512)

    assert image_size(payload) == (512, 512)  # no scaling, only re-encoding
    assert len(payload) < len(heavy)
    assert mime == "image/jpeg"


def test_re_encoding_never_sends_more_bytes_than_the_original() -> None:
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    Image.effect_noise((64, 64), 128).convert("RGB").save(buffer, format="JPEG", quality=30)
    coarse = buffer.getvalue()  # cheap JPEG: re-encoding it at quality=95 grows it

    payload, _ = preview(coarse, max_edge=512, inline_limit=0, quality=95)

    assert payload == coarse


def test_a_broken_image_falls_back_to_the_original_bytes() -> None:
    broken = _png(2048, 2048)[:200]  # header parses, pixels do not
    payload, mime = preview(broken, max_edge=512)
    assert payload == broken
    assert mime == "image/png"


def test_image_block_carries_base64_of_the_scaled_bytes() -> None:
    block = image_block(_png(800, 400), max_edge=200)

    assert block.type == "image"
    assert block.mime_type in {"image/jpeg", "image/png"}
    decoded = base64.b64decode(block.data)
    assert image_size(decoded) is not None
    assert max(image_size(decoded) or (0, 0)) == 200


def test_resource_link_points_at_the_prompt_and_index() -> None:
    link = resource_link(
        "prompt-1", 2, {"filename": "out.png", "node": "9", "width": 512, "height": 768}
    )

    assert link.type == "resource_link"
    assert link.uri == "comfy://output/prompt-1/2"
    assert link.name == "out.png#2"
    assert link.mime_type == "image/png"
    assert "512x768" in (link.description or "")
    assert "node 9" in (link.description or "")
