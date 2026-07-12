# Storage codecs and previews

RadarVault requests indexed transparent PNG (`image/png8`) from NOAA by
default. Some WMS layers reject PNG8 even though the service advertises it;
`app.wms.fetch_png_bytes` retries that request once as ordinary `image/png`.
Both responses are validated as actual PNGs with the expected dimensions.
Callers that need the conservative path can pass `image_format="image/png"`.

`app.frame_codec` is deliberately independent from the archive catalog. It
provides three archive choices:

| Format | Extension | Characteristics |
| --- | --- | --- |
| `png` | `.png` | Keeps fetched source bytes unchanged. |
| `png8` | `.png` | Indexed PNG with a bounded 256-colour palette and transparency table. |
| `webp-lossless` | `.webp` | Exact RGBA pixels with substantially smaller files for most radar frames. |

Every `EncodedFrame` reports dimensions, media type, a source SHA-256, and a
separate stored SHA-256. Raw bytes are never overwritten by a codec operation.
`encode_preview_frame` emits a transparent WebP, preserves aspect ratio, and
never upscales. A 768px preview is a practical default for browser playback;
full-resolution archive frames remain available for export and analysis.

## Dry-run-first conversion CLI

The standalone CLI works against an image file or directory and does not need
the web server:

```bash
python -m app.compression_cli benchmark cache/KTBW/frames --limit 5
python -m app.compression_cli generate-previews cache/KTBW/frames --limit 2 --dry-run
python -m app.compression_cli generate-previews cache/KTBW/frames --apply
python -m app.compression_cli convert cache/KTBW/frames \
  --format webp-lossless --limit 20 --dry-run
python -m app.compression_cli convert cache/KTBW/frames \
  --format webp-lossless --apply --manifest data/codec-manifest.json
```

`generate-previews` and `convert` are dry-run by default. `--apply` writes to a
separate `previews/` or `converted-<format>/` directory unless `--output-dir`
is provided. Writes use a temporary file, `fsync`, and an atomic rename; the
destination is decoded and hash-verified before it is considered complete.
Source deletion is a separate opt-in (`--delete-source`) and cannot be used in
a dry run. A failed write therefore leaves the source frame readable.

The JSON output contains one record per input with source/output paths, source
and stored hashes, dimensions, media type, byte counts, and savings. An
explicit `--manifest` on an applied command persists the same records for
auditability. Re-running preview generation is safe and deterministic.

## Measuring a cache

For a representative comparison, benchmark each format against the same
fixture set:

```bash
python -m app.compression_cli benchmark cache/KTBW/frames --format png
python -m app.compression_cli benchmark cache/KTBW/frames --format png8
python -m app.compression_cli benchmark cache/KTBW/frames --format webp-lossless
```

The benchmark is advisory: NOAA imagery and product layers change over time,
so production decisions should record the generated JSON output and preserve
the original frames until a verified migration has been reviewed.

