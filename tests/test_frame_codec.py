from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.compression_cli import convert, generate_previews
from app.frame_codec import (
    FrameCodecError,
    decode_to_rgba,
    encode_archive_frame,
    encode_preview_frame,
    probe_image,
)


def _png(size: tuple[int, int] = (128, 96), *, many_colors: bool = False) -> bytes:
    image = Image.new("RGBA", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if many_colors:
                pixels[x, y] = ((x * 17) % 256, (y * 23) % 256, (x + y) % 256, 255)
            else:
                pixels[x, y] = ((x // 12) % 2 * 180, (y // 12) % 2 * 200, 240, 0 if (x + y) % 9 else 220)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_archive_codecs_keep_dimensions_and_hash_provenance():
    source = _png()
    source_hash = hashlib.sha256(source).hexdigest()
    for archive_format in ("png", "png8", "webp-lossless"):
        encoded = encode_archive_frame(source, archive_format=archive_format)
        assert encoded.source_sha256 == source_hash
        assert encoded.stored_sha256 == hashlib.sha256(encoded.data).hexdigest()
        assert (encoded.width, encoded.height) == (128, 96)
        assert decode_to_rgba(encoded.data).size == (128, 96)
        assert probe_image(encoded.data)["width"] == 128


def test_webp_lossless_preserves_visible_rgba_pixels():
    source = _png((48, 32))
    expected = Image.open(BytesIO(source)).convert("RGBA")
    encoded = encode_archive_frame(source, archive_format="webp-lossless")
    actual = decode_to_rgba(encoded.data)
    # WebP is allowed to canonicalize RGB channels where alpha is zero; all
    # visible (alpha > 0) RGBA values must remain exact.
    assert [pixel for pixel in actual.getdata() if pixel[3]] == [
        pixel for pixel in expected.getdata() if pixel[3]
    ]
    assert probe_image(encoded.data)["has_alpha"] is True


def test_preview_bounds_aspect_ratio_and_transparency():
    source = _png((400, 200))
    encoded = encode_preview_frame(source, max_dimension=80, quality=82)
    assert (encoded.width, encoded.height) == (80, 40)
    assert encoded.extension == ".webp"
    assert probe_image(encoded.data)["has_alpha"] is True
    assert decode_to_rgba(encoded.data).size == (80, 40)


def test_png8_or_webp_reduces_synthetic_radar_fixture():
    source = _png((512, 512), many_colors=True)
    png8 = encode_archive_frame(source, archive_format="png8")
    webp = encode_archive_frame(source, archive_format="webp-lossless")
    assert min(len(png8.data), len(webp.data)) <= len(source) * 0.60


def test_invalid_codec_arguments_are_rejected():
    with pytest.raises(FrameCodecError):
        encode_archive_frame(_png(), archive_format="jpeg")
    with pytest.raises(FrameCodecError):
        encode_preview_frame(_png(), max_dimension=0)
    with pytest.raises(FrameCodecError):
        encode_preview_frame(_png(), quality=101)


def test_preview_generation_is_idempotent_and_dry_run_does_not_write(tmp_path: Path):
    source_dir = tmp_path / "frames"
    source_dir.mkdir()
    source = source_dir / "20260101_000000Z.png"
    source.write_bytes(_png())
    output = tmp_path / "previews"

    assert generate_previews(
        source_dir,
        limit=None,
        output_dir=output,
        max_dimension=64,
        quality=82,
        apply=False,
        manifest=None,
    ) == 0
    assert not output.exists()

    assert generate_previews(
        source_dir,
        limit=None,
        output_dir=output,
        max_dimension=64,
        quality=82,
        apply=True,
        manifest=None,
    ) == 0
    preview = output / source.name.replace(".png", ".webp")
    first = preview.read_bytes()
    assert preview.exists()
    assert generate_previews(
        source_dir,
        limit=None,
        output_dir=output,
        max_dimension=64,
        quality=82,
        apply=True,
        manifest=None,
    ) == 0
    assert preview.read_bytes() == first
    assert source.exists()


def test_conversion_failure_keeps_original(monkeypatch, tmp_path: Path):
    source_dir = tmp_path / "frames"
    source_dir.mkdir()
    source = source_dir / "frame.png"
    original = _png()
    source.write_bytes(original)

    def fail_write(*_args, **_kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr("app.compression_cli._atomic_write", fail_write)
    with pytest.raises(OSError):
        convert(
            source_dir,
            limit=None,
            archive_format="webp-lossless",
            output_dir=tmp_path / "converted",
            apply=True,
            delete_source=True,
            manifest=None,
        )
    assert source.read_bytes() == original
    assert not (tmp_path / "converted" / "frame.webp").exists()
