"""Digest core: exact quantiles, determinism, truncation, no floats."""
import pytest

from finfacts.model import FinFact, Period, Source, canonical_json
from finlens.digest import (build_digest, metric_summary, quantile_lower,
                            render_scaled)

SRC = Source(kind="test-fixture", ref="t1", fetched="2026-01-01")


def _fact(entity, concept="finfield:revenue_ttm", value=0, scale=0,
          end="2025-12-31", unit="USD"):
    return FinFact(entity_id=f"ticker:{entity}", concept=concept, value=value,
                   scale=scale, unit=unit, period=Period(end=end), source=SRC)


# 1 — the quantile rule: lower interpolation, element (num*(n-1))//den
def test_quantile_rule_exact():
    assert quantile_lower([1, 2, 3, 4], 1, 4) == 1
    assert quantile_lower([1, 2, 3, 4], 1, 2) == 2   # even n -> lower middle
    assert quantile_lower([1, 2, 3, 4], 3, 4) == 3
    assert quantile_lower([10, 20, 30, 40, 50], 1, 4) == 20
    assert quantile_lower([10, 20, 30, 40, 50], 1, 2) == 30
    assert quantile_lower([10, 20, 30, 40, 50], 3, 4) == 40
    assert quantile_lower([10, 20], 1, 4) == 10
    assert quantile_lower([10, 20], 1, 2) == 10
    assert quantile_lower([10, 20], 3, 4) == 10      # (3*1)//4 == 0
    assert quantile_lower([7], 3, 4) == 7
    with pytest.raises(ValueError):
        quantile_lower([], 1, 2)
    with pytest.raises(ValueError):
        quantile_lower([1], 5, 4)


def test_metric_summary_renders_plain_decimal_strings():
    s = metric_summary([100000, 300000, 200000], scale=6)
    assert s == {"median": "0.2", "p25": "0.1", "p75": "0.2", "n": 3}
    assert metric_summary([100], scale=0)["median"] == "100"
    assert metric_summary([0], scale=6)["median"] == "0"


def test_render_scaled_is_exact_at_any_digit_count():
    # 32 significant digits at scale 0 — beyond Decimal's default 28-digit
    # context, so any context-based rendering would round; ours must not
    big = 12345678901234567890123456789012
    assert render_scaled(big, 0) == "12345678901234567890123456789012"
    assert render_scaled(-big, 0) == "-12345678901234567890123456789012"
    # high scale: exact zero-padding, no scientific notation
    assert render_scaled(big, 30) == "12.345678901234567890123456789012"
    assert render_scaled(1, 30) == "0.000000000000000000000000000001"
    # trailing fractional zeros strip; zero stays "0" at every scale
    assert render_scaled(1230, 2) == "12.3"
    assert render_scaled(-2500, 3) == "-2.5"
    assert render_scaled(0, 12) == "0"
    assert render_scaled(31000, 6) == "0.031"


# 2 — mixed input scales normalize to the finest common scale, exactly
def test_mixed_scales():
    facts = [
        _fact("A", value=1, scale=0),          # 1
        _fact("B", value=1500000, scale=6),    # 1.5
        _fact("C", value=2, scale=0),          # 2
    ]
    d = build_digest("test", {}, {"g": facts})
    m = d["groups"][0]["metrics"]["finfield:revenue_ttm"]["USD"]
    assert m == {"median": "1.5", "p25": "1", "p75": "1.5", "n": 3}


# 2b — units never mix: one concept in USD and JPY yields two summaries
def test_units_partition_aggregation():
    facts = [
        _fact("A", value=100, unit="USD"),
        _fact("B", value=300, unit="USD"),
        _fact("C", value=15000000, unit="JPY"),
    ]
    d = build_digest("test", {}, {"g": facts})
    m = d["groups"][0]["metrics"]["finfield:revenue_ttm"]
    assert m["USD"] == {"median": "100", "p25": "100", "p75": "100", "n": 2}
    assert m["JPY"] == {"median": "15000000", "p25": "15000000",
                        "p75": "15000000", "n": 1}
    # all three facts are cited, whatever their unit
    assert len(d["groups"][0]["citations"]) == 3


# 3 — determinism: input order never leaks into the bytes
def test_two_builds_byte_identical():
    facts = [_fact(t, value=v) for t, v in (("A", 30), ("B", 10), ("C", 20))]
    d1 = build_digest("test", {"group_by": "x"}, {"g2": facts, "g1": [facts[0]]})
    d2 = build_digest("test", {"group_by": "x"},
                      {"g1": [facts[0]], "g2": list(reversed(facts))})
    assert canonical_json(d1) == canonical_json(d2)
    assert d1["state_root"] is None
    assert d1["unclassified"] == {"entities": 0}
    assert d1["out_of_universe"] == 0


# 4 — caps never truncate silently, and say per kind what they dropped
def test_truncation_marker():
    grouped = {
        "a": [_fact("A1", value=1), _fact("A2", value=2), _fact("A3", value=3)],
        "b": [_fact("B1", value=1)],
        "c": [_fact("C1", value=1)],
    }
    d = build_digest("test", {}, grouped, max_groups=2, max_citations_per_group=2)
    # group "c" dropped (1) + one of "a"'s three citations dropped (1)
    assert d["truncated"] == {"groups": 1, "citations": 1}
    assert [g["key"] for g in d["groups"]] == ["a", "b"]
    assert len(d["groups"][0]["citations"]) == 2

    untruncated = build_digest("test", {}, grouped)
    assert "truncated" not in untruncated
    assert len(untruncated["groups"]) == 3


# 5 — the no-float property: every leaf is int, str, or None
def _leaves(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str)
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def test_no_floats_anywhere():
    grouped = {"g": [_fact("A", value=12345, scale=2),
                     _fact("B", concept="finfield:net_margin_ttm",
                           value=250000, scale=6, unit="pure")]}
    d = build_digest("lens", {"group_by": "g"}, grouped,
                     unclassified={"entities": 1})
    for leaf in _leaves(d):
        assert not isinstance(leaf, (float, bool))
        assert leaf is None or isinstance(leaf, (int, str))
