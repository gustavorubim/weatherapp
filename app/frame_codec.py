"""Deterministic image codecs used by the archive and playback preview paths.

The collector receives PNG bytes from NOAA.  This module keeps the source and
stored hashes separate, validates every encoded result, and never mutates the
source bytes.  It intentionally has no dependency on the storage/catalog
layers, making it safe to use during migrations and in a standalone CLI.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image


class FrameCodecError(ValueError):
    """Raised when a frame cannot be decoded or an unsupported codec is used."""


@dataclass(frozen=True)
class EncodedFrame:
    data: bytes
    extension: str
    media_type: str
    width: int
    height: int
    source_sha256: str
    stored_sha256: str


_MEDIA_TYPES = {
    "png": "image/png",
    "png8": "image/png",
    "webp-lossless": "image/webp",
    "webp": "image/webp",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_image(data: bytes) -> Image.Image:
    if not data:
        raise FrameCodecError("frame data is empty")
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            # A copy is required because the BytesIO and image context are
            # deliberately closed before a caller receives the image.
            return source.copy()
    except Exception as exc:  # noqa: BLE001
        raise FrameCodecError(f"invalid image data: {exc}") from exc


def decode_to_rgba(data: bytes) -> Image.Image:
    """Decode *data* into a detached RGBA Pillow image."""

    return _open_image(data).convert("RGBA")


def probe_image(data: bytes) -> dict[str, object]:
    """Return stable metadata for an encoded frame without exposing Pillow."""

    image = _open_image(data)
    fmt = (image.format or "").lower() if image.format else ""
    # Pillow's copy() does not always retain ``format``; inspect a second
    # handle for metadata while retaining the detached, loaded image above.
    try:
        with Image.open(io.BytesIO(data)) as opened:
            fmt = (opened.format or fmt).lower()
            mode = opened.mode
            width, height = opened.size
            has_alpha = "A" in mode or "transparency" in opened.info
    except Exception as exc:  # noqa: BLE001
        raise FrameCodecError(f"invalid image data: {exc}") from exc
    media_type = {"png": "image/png", "webp": "image/webp", "jpeg": "image/jpeg"}.get(
        fmt, f"image/{fmt}" if fmt else "application/octet-stream"
    )
    return {
        "format": fmt,
        "media_type": media_type,
        "mode": mode,
        "width": width,
        "height": height,
        "has_alpha": has_alpha,
        "bytes": len(data),
    }


def _encode_png8(image: Image.Image) -> bytes:
    """Encode an RGBA image as indexed PNG, retaining transparency.

    Pillow's RGBA FASTOCTREE path can turn near-identical opaque colours into
    a transparent palette entry.  Use an exact palette whenever the fixture
    has at most 256 RGBA colours, then use RGB quantization plus a conservative
    per-entry alpha average for larger weather images.
    """

    rgba = image.convert("RGBA")
    colors = rgba.getcolors(maxcolors=257)
    if colors is not None and len(colors) <= 256:
        colors.sort(key=lambda item: item[1])
        palette_index = {pixel: index for index, (_count, pixel) in enumerate(colors)}
        indexed = Image.new("P", rgba.size)
        indexed.putdata([palette_index[pixel] for pixel in rgba.getdata()])
        rgb_palette: list[int] = []
        alpha_table: list[int] = []
        for _count, pixel in colors:
            rgb_palette.extend(pixel[:3])
            alpha_table.append(pixel[3])
        indexed.putpalette(rgb_palette + [0] * (768 - len(rgb_palette)), rawmode="RGB")
        if any(alpha < 255 for alpha in alpha_table):
            indexed.info["transparency"] = bytes(alpha_table)
    else:
        # Quantize RGB (the only reliable Pillow path across supported
        # versions), then attach an average alpha per resulting palette entry.
        indexed = rgba.convert("RGB").quantize(
            colors=256,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        )
        alpha_sums = [0] * 256
        alpha_counts = [0] * 256
        for index, pixel in zip(indexed.getdata(), rgba.getdata()):
            alpha_sums[index] += pixel[3]
            alpha_counts[index] += 1
        alpha_table = bytes(
            round(alpha_sums[index] / alpha_counts[index]) if alpha_counts[index] else 255
            for index in range(256)
        )
        if any(alpha < 255 for alpha in alpha_table):
            indexed.info["transparency"] = alpha_table
    output = io.BytesIO()
    indexed.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _encoded(
    data: bytes,
    *,
    extension: str,
    media_type: str,
    source: bytes,
    width: int,
    height: int,
) -> EncodedFrame:
    # Verify dimensions and decodability before making the result visible to a
    # caller or an atomic archive writer.
    decoded = _open_image(data)
    if decoded.size != (width, height):
        raise FrameCodecError(
            f"encoded frame dimensions changed from {(width, height)} to {decoded.size}"
        )
    return EncodedFrame(
        data=data,
        extension=extension,
        media_type=media_type,
        width=width,
        height=height,
        source_sha256=_sha256(source),
        stored_sha256=_sha256(data),
    )


def encode_archive_frame(source_png: bytes, *, archive_format: str) -> EncodedFrame:
    """Encode a source PNG as ``png``, ``png8``, or ``webp-lossless``."""

    fmt = archive_format.strip().lower()
    aliases = {"webp": "webp-lossless", "image/png": "png", "image/png8": "png8"}
    fmt = aliases.get(fmt, fmt)
    if fmt not in {"png", "png8", "webp-lossless"}:
        raise FrameCodecError(
            f"unsupported archive format {archive_format!r}; expected png, png8, or webp-lossless"
        )
    image = _open_image(source_png)
    width, height = image.size
    if fmt == "png":
        # Preserve the fetched bytes exactly.  This avoids a needless rewrite
        # when the user chooses the compatibility/default archive format.
        data = source_png
        extension = ".png"
    elif fmt == "png8":
        data = _encode_png8(image.convert("RGBA"))
        extension = ".png"
    else:
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="WEBP", lossless=True, method=6)
        data = output.getvalue()
        extension = ".webp"
    return _encoded(
        data,
        extension=extension,
        media_type=_MEDIA_TYPES[fmt],
        source=source_png,
        width=width,
        height=height,
    )


def encode_preview_frame(
    source_png: bytes,
    *,
    max_dimension: int = 768,
    quality: int = 82,
) -> EncodedFrame:
    """Create a bounded-size transparent WebP preview without upscaling."""

    if max_dimension < 1:
        raise FrameCodecError("max_dimension must be at least 1")
    if not 0 <= quality <= 100:
        raise FrameCodecError("quality must be between 0 and 100")
    image = _open_image(source_png).convert("RGBA")
    width, height = image.size
    longest = max(width, height)
    if longest > max_dimension:
        scale = max_dimension / longest
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = image.resize(target, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="WEBP", quality=quality, method=6)
    data = output.getvalue()
    return EncodedFrame(
        data=data,
        extension=".webp",
        media_type="image/webp",
        width=image.width,
        height=image.height,
        source_sha256=_sha256(source_png),
        stored_sha256=_sha256(data),
    )
