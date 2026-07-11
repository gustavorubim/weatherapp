from __future__ import annotations

from app.config import radar_bbox_3857
from app.products import PRODUCT_PREFERENCE
from app.wms import layer_name, workspace_wms_url


def test_layer_name_wsr88d(monkeypatch):
    monkeypatch.setattr("app.wms.preferred_product", lambda rid: "sr_bref")
    assert layer_name("KTBW") == "ktbw:ktbw_sr_bref"
    assert workspace_wms_url("KTBW").endswith("/ktbw/wms")


def test_layer_name_tdwr(monkeypatch):
    monkeypatch.setattr("app.wms.preferred_product", lambda rid: "bref1")
    assert layer_name("TMCO") == "tmco:tmco_bref1"


def test_product_preference_order():
    assert PRODUCT_PREFERENCE[0] == "sr_bref"
    assert "bref1" in PRODUCT_PREFERENCE


def test_bbox_centered():
    bbox = radar_bbox_3857(-82.40194, 27.70528)
    assert len(bbox) == 4
    assert bbox[0] < bbox[2]
    assert bbox[1] < bbox[3]
