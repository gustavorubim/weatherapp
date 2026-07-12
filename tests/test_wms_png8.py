from __future__ import annotations

from io import BytesIO

import httpx
from PIL import Image

from app.wms import _validate_png_bytes, build_getmap_url, fetch_png_bytes


def _png_bytes(size=(4, 3), *, indexed=False) -> bytes:
    image = Image.new("P" if indexed else "RGBA", size)
    if indexed:
        image.putdata([0, 1, 2, 3] * (size[0] * size[1] // 4))
        image.info["transparency"] = bytes([0, 100, 180, 255])
    else:
        image.putdata([(20, 30, 40, 0 if i % 3 == 0 else 255) for i in range(size[0] * size[1])])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _Response:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="replace")


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        return self.responses.pop(0)


def test_build_getmap_url_requests_png8_by_default(monkeypatch):
    monkeypatch.setattr("app.wms.preferred_product", lambda _rid: "sr_bref")
    url = build_getmap_url("KTBW", lon=-82.4, lat=27.7, width=4, height=3)
    assert httpx.URL(url).params["format"] == "image/png8"
    png_url = build_getmap_url(
        "KTBW", lon=-82.4, lat=27.7, width=4, height=3, format="png"
    )
    assert httpx.URL(png_url).params["format"] == "image/png"


def test_validate_png_accepts_indexed_transparency():
    data = _png_bytes(indexed=True)
    image = _validate_png_bytes(data, expected_size=(4, 3))
    assert image.mode == "P"


def test_png8_rejection_retries_once_as_png(monkeypatch):
    monkeypatch.setattr("app.wms.get_radar", lambda _rid: {"lon": -82.4, "lat": 27.7})
    monkeypatch.setattr("app.wms.resolve_product", lambda _rid, _product=None: "sr_bref")
    png = _png_bytes()
    client = _Client([_Response(b"<?xml version='1.0'?><ServiceException>unsupported</ServiceException>"), _Response(png)])
    data, _bbox, product = fetch_png_bytes(
        "KTBW", width=4, height=3, client=client
    )
    assert data == png
    assert product == "sr_bref"
    assert len(client.urls) == 2
    assert httpx.URL(client.urls[0]).params["format"] == "image/png8"
    assert httpx.URL(client.urls[1]).params["format"] == "image/png"


def test_png8_http_error_retries_and_preserves_signature(monkeypatch):
    monkeypatch.setattr("app.wms.get_radar", lambda _rid: {"lon": -82.4, "lat": 27.7})
    monkeypatch.setattr("app.wms.resolve_product", lambda _rid, _product=None: "sr_bref")
    png = _png_bytes()
    client = _Client([_Response(b"no", status_code=400), _Response(png)])
    data, bbox, product = fetch_png_bytes("KTBW", width=4, height=3, client=client)
    assert data == png
    assert len(bbox) == 4
    assert product == "sr_bref"

