"""ComfyUI output images -> MCP content blocks.

The economics of this module: a 1024x1024 PNG is roughly 1.5 MB, which becomes about
2 MB once base64-encoded into a JSON frame, for an image the model will downsample
anyway. So the first image travels inline scaled to preview_max_edge, and every image
stays addressable as a comfy://output/<prompt_id>/<index> resource link.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from mcp.types import ImageContent, ResourceLink

log = logging.getLogger(__name__)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")
_JPEG_SOF = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def image_mime(data: bytes) -> str | None:
    """Sniff the media type from magic bytes; filenames lie, /view redirects do not."""
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(_GIF_MAGIC):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height straight from the header, without decoding the pixels."""
    if data.startswith(PNG_MAGIC) and len(data) >= 24:
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    if data.startswith(_GIF_MAGIC) and len(data) >= 10:
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    if data.startswith(b"BM") and len(data) >= 26:
        return (
            int.from_bytes(data[18:22], "little"),
            abs(int.from_bytes(data[22:26], "little", signed=True)),
        )
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    end = len(data) - 9
    while index < end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            index += 2 if marker != 0xFF else 1
            continue
        segment = int.from_bytes(data[index + 2 : index + 4], "big")
        if marker in _JPEG_SOF:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + segment
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(data) >= 30:
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    return None


INLINE_LIMIT = 48_000


def preview(
    data: bytes,
    *,
    max_edge: int = 512,
    quality: int = 80,
    inline_limit: int = INLINE_LIMIT,
) -> tuple[bytes, str]:
    """Shrink an output image to something worth putting inside a JSON frame.

    Two separate reasons to re-encode, and a 512x512 ComfyUI PNG hits the second one:

    * geometry: the long edge is above max_edge, so scale it down;
    * weight: the file itself is above inline_limit, so re-encode it even when the
      pixels are already small. A 512x512 anime PNG is ~370 KB; the same picture as
      JPEG is ~40 KB, and the model will not see the difference.

    Falling back to the original bytes is deliberate: a preview that requires Pillow
    is a preview that breaks the server on a machine without it.
    """
    mime = image_mime(data) or "application/octet-stream"
    size = image_size(data)
    if size is None:
        return data, mime
    shrink = max(size) > max_edge
    if not shrink and len(data) <= inline_limit:
        return data, mime
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on the install
        log.debug("Pillow is not installed; sending the original image bytes")
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as image:
            resample = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail((max_edge, max_edge), resample)
            buffer = io.BytesIO()
            if _has_alpha(image):
                image.convert("RGBA").save(buffer, format="PNG", optimize=True)
                encoded, encoded_mime = buffer.getvalue(), "image/png"
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
                encoded, encoded_mime = buffer.getvalue(), "image/jpeg"
            if len(encoded) >= len(data):
                # Re-encoding made it bigger (already-optimised JPEG, for instance):
                # the point of this function is to save bytes, so keep the original.
                return data, mime
            return encoded, encoded_mime
    except Exception as exc:  # noqa: BLE001 - a broken preview must not break a run
        log.warning("preview resize failed (%s); sending the original bytes", exc)
        return data, mime


def _has_alpha(image: Any) -> bool:
    return image.mode in {"RGBA", "LA", "PA"} or (
        image.mode in {"P", "L"} and "transparency" in image.info
    )


def image_block(data: bytes, *, max_edge: int = 512) -> ImageContent:
    """One inline image block, already scaled."""
    payload, mime = preview(data, max_edge=max_edge)
    return ImageContent(
        type="image",
        data=base64.b64encode(payload).decode("ascii"),
        mime_type=mime,
    )


def resource_link(prompt_id: str, index: int, output: dict[str, Any]) -> ResourceLink:
    """A stable pointer to one output image, readable via comfy://output/..."""
    return ResourceLink(
        type="resource_link",
        name=f"{output.get('filename') or 'output'}#{index}",
        uri=f"comfy://output/{prompt_id}/{index}",
        mime_type="image/png",
        description=f"{output.get('width')}x{output.get('height')} from node {output.get('node')}",
    )
