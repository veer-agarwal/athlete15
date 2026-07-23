"""Tests for the weather source.

Run with:  pytest

Note this hits the live API, which makes it an integration test rather than a unit
test. That is acceptable for phase 1. When you get to phase 4 and are testing the
lift command parser, those should be real unit tests with no network at all.
"""

from src.sources import weather


def test_fetch_returns_expected_keys():
    result = weather.fetch()
    assert len(result) == 1
    for key in ("temp_now_f", "high_f", "low_f", "precip_chance", "condition"):
        assert key in result[0]


def test_high_is_not_below_low():
    w = weather.fetch()[0]
    assert w["high_f"] >= w["low_f"]
