from __future__ import annotations

import pytest

from app.cache_cli import format_duration, parse_duration


def test_parse_duration_plain_seconds():
    assert parse_duration("90") == 90
    assert parse_duration("90s") == 90


def test_parse_duration_human():
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("2d") == 172800
    assert parse_duration("1d12h") == 129600
    assert parse_duration("48h") == 172800


def test_parse_duration_invalid():
    with pytest.raises(Exception):
        parse_duration("nope")


def test_format_duration():
    assert format_duration(172800) == "2d"
    assert format_duration(90) == "1m30s"
