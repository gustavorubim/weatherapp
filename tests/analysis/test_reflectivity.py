from __future__ import annotations

import numpy as np
from PIL import Image

from app.analysis.fixtures import synthetic_moving_cell
from app.analysis.reflectivity import (
    NODATA,
    UNKNOWN,
    decode_reflectivity_bins,
    documented_palette,
    paint_bins,
)
from app.analysis.provenance import sha256_array


def test_palette_documented_and_roundtrip():
    palette = documented_palette()
    assert len(palette) >= 10
    assert palette[0].bin == 0
    assert palette[0].approx_dbz > 0

    bins = np.full((32, 32), NODATA, dtype=np.int16)
    bins[8:16, 8:16] = 6
    img = paint_bins(bins)
    decoded = decode_reflectivity_bins(img)
    assert isinstance(decoded, np.ndarray)
    assert decoded.dtype == np.int16
    assert np.all(decoded[8:16, 8:16] == 6)
    assert np.all(decoded[0:4, 0:4] == NODATA)


def test_unknown_opaque_color_handled():
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    # Distinct opaque color far from palette anchors.
    for y in range(8):
        for x in range(8):
            img.putpixel((x, y), (90, 40, 10, 255))
    decoded = decode_reflectivity_bins(img, match_tolerance=20)
    assert isinstance(decoded, np.ndarray)
    assert np.all(decoded == UNKNOWN)


def test_provenance_records_hashes_and_params():
    fixture = synthetic_moving_cell(size=48, n_frames=2)
    img = fixture["images"][0]
    result = decode_reflectivity_bins(img, return_provenance=True)
    assert isinstance(result, tuple)
    bins, prov = result
    assert prov["kind"] == "reflectivity_bins"
    assert prov["source_hashes"]
    assert "palette" in prov["parameters"]
    assert sha256_array(bins)


def test_bytes_input_decodes():
    fixture = synthetic_moving_cell(size=40, n_frames=1)
    from io import BytesIO

    buf = BytesIO()
    fixture["images"][0].save(buf, format="PNG")
    decoded = decode_reflectivity_bins(buf.getvalue())
    assert isinstance(decoded, np.ndarray)
    assert np.any(decoded >= 0)
