"""FactorLens: hand-computed deciles, sector blocks, deterministic top/bottom.

Main universe: 10 entities T00..T09 with factor values 10..100 (scale 0,
unit "pure"). Lower-interpolation decile boundaries over n=10 sorted values
sit at index (i*9)//10, so:

    boundaries = [10, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

T00..T04 carry sic 3674 (Manufacturing), T05..T07 sic 6022 (Finance),
T08/T09 no sic — they group visibly under "unclassified".
"""
import pytest

from finfacts.model import Entity, FinFact, Period, Source, canonical_json
from finlens.adapter import HAS_PULSE, snapshot_digest
from finlens.lenses import FactorLens

requires_pulse = pytest.mark.skipif(not HAS_PULSE, reason="knitweb (pulse) not installed")

SRC = Source(kind="test-fixture", ref="t1", fetched="2026-01-01")
FACTOR = "finfield:book_to_float_mcap"


def _fact(ticker, concept, value, scale=0, unit="pure", end="2025-12-31"):
    return FinFact(entity_id=f"ticker:{ticker}", concept=concept, value=value,
                   scale=scale, unit=unit, period=Period(end=end), source=SRC)


def _universe():
    entities, facts = [], []
    for i in range(10):
        ticker = f"T{i:02d}"
        entities.append(Entity(ticker=ticker))
        facts.append(_fact(ticker, FACTOR, 10 * (i + 1)))
        if i < 5:
            facts.append(_fact(ticker, "finfield:sic", 3674))
        elif i < 8:
            facts.append(_fact(ticker, "finfield:sic", 6022))
    # a stale factor fact for T00 — the latest (2025-12-31) one must win
    facts.append(_fact("T00", FACTOR, 999999, end="2025-06-30"))
    return entities, facts


def _group(digest, key):
    return next(g for g in digest["groups"] if g["key"] == key)


ORIENTATION = "higher = cheaper (book value per unit of free-float market cap)"
STALENESS = "ratio inputs may differ by up to 400 days (derive guard)"


def test_factor_deciles_hand_computed():
    entities, facts = _universe()
    d = FactorLens().build(facts, entities, max_citations_per_group=16)
    assert d["kind"] == "finfield-lens-digest"
    assert d["lens"] == "factor"
    # d1=10, d9=90 -> fences 10-3*80=-230 / 90+3*80=330, rendered exact
    assert d["scope"] == {"factor": FACTOR, "unit": "pure", "top_n": 5,
                          "orientation": ORIENTATION,
                          "staleness_note": STALENESS,
                          "fences": {"low": "-230", "high": "330"}}
    assert d["deciles"] == ["10", "10", "20", "30", "40", "50",
                            "60", "70", "80", "90", "100"]
    assert d["outliers"] == {"count": 0, "high": [], "low": []}
    assert d["unclassified"] == {"entities": 0}
    assert d["out_of_universe"] == 0
    assert "truncated" not in d


def test_factor_sector_groups_and_citations():
    entities, facts = _universe()
    d = FactorLens().build(facts, entities, max_citations_per_group=16)
    # sorted keys; entities without a sic fact group VISIBLY as "unclassified"
    assert [(g["key"], g["entities"]) for g in d["groups"]] == [
        ("Finance, Insurance & Real Estate", 3), ("Manufacturing", 5),
        ("unclassified", 2)]

    manu = _group(d, "Manufacturing")  # values 10,20,30,40,50
    assert manu["metrics"] == {FACTOR: {"pure": {
        "median": "30", "p25": "20", "p75": "40", "n": 5}}}
    # 5 latest factor facts + 5 classifying sic facts; the stale T00 factor
    # fact is not cited
    assert len(manu["citations"]) == 10
    stale = _fact("T00", FACTOR, 999999, end="2025-06-30")
    fresh = _fact("T00", FACTOR, 10)
    assert stale.cid not in manu["citations"]
    assert fresh.cid in manu["citations"]
    sic = _fact("T00", "finfield:sic", 3674)
    assert sic.cid in manu["citations"]

    fin = _group(d, "Finance, Insurance & Real Estate")  # 60,70,80
    assert fin["metrics"][FACTOR]["pure"] == {
        "median": "70", "p25": "60", "p75": "70", "n": 3}

    uncl = _group(d, "unclassified")  # 90,100 — no sic citations
    assert uncl["metrics"][FACTOR]["pure"] == {
        "median": "90", "p25": "90", "p75": "90", "n": 2}
    assert len(uncl["citations"]) == 2


def test_factor_top_bottom():
    entities, facts = _universe()
    d = FactorLens().build(facts, entities, max_citations_per_group=16)
    assert [(e["entity"], e["value"]) for e in d["top"]] == [
        ("ticker:T09", "100"), ("ticker:T08", "90"), ("ticker:T07", "80"),
        ("ticker:T06", "70"), ("ticker:T05", "60")]
    assert [(e["entity"], e["value"]) for e in d["bottom"]] == [
        ("ticker:T00", "10"), ("ticker:T01", "20"), ("ticker:T02", "30"),
        ("ticker:T03", "40"), ("ticker:T04", "50")]
    # every ranked entity carries its factor fact's citation and period_end,
    # so stale-instant mixing across the cross-section is visible
    assert d["bottom"][0]["citation"] == _fact("T00", FACTOR, 10).cid
    assert all(e["period_end"] == "2025-12-31" for e in d["top"] + d["bottom"])


def test_factor_tie_break_is_value_then_entity_id():
    facts = [_fact("MM", FACTOR, 5), _fact("ZZ", FACTOR, 5),
             _fact("AA", FACTOR, 5), _fact("BB", FACTOR, 7),
             _fact("NN", FACTOR, 7)]
    d = FactorLens(top_n=2).build(facts)
    assert [(e["entity"], e["value"]) for e in d["top"]] == [
        ("ticker:BB", "7"), ("ticker:NN", "7")]  # ties: ascending entity_id
    assert [(e["entity"], e["value"]) for e in d["bottom"]] == [
        ("ticker:AA", "5"), ("ticker:MM", "5")]
    # byte-identical regardless of input order
    d2 = FactorLens(top_n=2).build(list(reversed(facts)))
    assert canonical_json(d) == canonical_json(d2)


def test_factor_mixed_scales_common_scale():
    facts = [_fact("AA", FACTOR, 15, scale=1),  # 1.5
             _fact("BB", FACTOR, 2),            # 2
             _fact("CC", FACTOR, 25, scale=1)]  # 2.5
    d = FactorLens().build(facts)
    assert d["deciles"][0] == "1.5" and d["deciles"][-1] == "2.5"
    assert [e["value"] for e in d["top"]] == ["2.5", "2", "1.5"]
    # fences render exact at the common scale: d1=1.5, d9=2 -> 0 / 3.5
    assert d["scope"]["fences"] == {"low": "0", "high": "3.5"}


def test_factor_foreign_unit_and_missing_factor_are_unclassified():
    entities = [Entity(ticker="AA"), Entity(ticker="BB"), Entity(ticker="CC")]
    facts = [_fact("AA", FACTOR, 5),
             _fact("BB", FACTOR, 7, unit="USD"),      # foreign unit
             _fact("CC", "finfield:revenue_ttm", 9)]  # no factor fact
    d = FactorLens().build(facts, entities)
    assert d["unclassified"] == {"entities": 2}
    assert d["deciles"] == ["5"] * 11
    assert [e["entity"] for e in d["top"]] == ["ticker:AA"]


def test_factor_empty_cross_section():
    d = FactorLens().build([], [Entity(ticker="AA")])
    assert d["deciles"] == [] and d["top"] == [] and d["bottom"] == []
    assert d["outliers"] == {"count": 0, "high": [], "low": []}
    assert "fences" not in d["scope"]  # nothing to fence
    assert d["scope"]["orientation"] == ORIENTATION
    assert d["groups"] == []
    assert d["unclassified"] == {"entities": 1}


def test_factor_outliers_fenced_out_of_top_bottom_but_never_dropped():
    # the real-feed pathology: absurd degenerate ratios dominate top/bottom
    entities, facts = _universe()
    entities += [Entity(ticker="XHI"), Entity(ticker="XLO")]
    facts += [_fact("XHI", FACTOR, 10**7), _fact("XLO", FACTOR, -(10**7))]
    d = FactorLens().build(facts, entities, max_citations_per_group=32)
    # n=12 sorted: d1 = idx (1*11)//10 = 1 -> 10, d9 = idx (9*11)//10 = 9 -> 90
    assert d["scope"]["fences"] == {"low": "-230", "high": "330"}
    # top/bottom rank inliers only — the absurd values never appear
    assert [(e["entity"], e["value"]) for e in d["top"]] == [
        ("ticker:T09", "100"), ("ticker:T08", "90"), ("ticker:T07", "80"),
        ("ticker:T06", "70"), ("ticker:T05", "60")]
    assert [(e["entity"], e["value"]) for e in d["bottom"]] == [
        ("ticker:T00", "10"), ("ticker:T01", "20"), ("ticker:T02", "30"),
        ("ticker:T03", "40"), ("ticker:T04", "50")]
    # ...but never disappear: the outliers section carries them, count exact
    assert d["outliers"]["count"] == 2
    assert d["outliers"]["high"] == [{
        "entity": "ticker:XHI", "value": "10000000",
        "period_end": "2025-12-31",
        "citation": _fact("XHI", FACTOR, 10**7).cid}]
    assert d["outliers"]["low"] == [{
        "entity": "ticker:XLO", "value": "-10000000",
        "period_end": "2025-12-31",
        "citation": _fact("XLO", FACTOR, -(10**7)).cid}]
    # outliers still aggregate in their sector group (deciles include them)
    assert d["deciles"][0] == "-10000000" and d["deciles"][-1] == "10000000"


def test_factor_outlier_lists_cap_at_three_count_stays_exact():
    # 40 sane values 1..40, then 4 high outliers: d1=5, d9=39 -> hi=141,
    # so all 4 are fenced out; only the 3 most extreme are listed but the
    # count stays exact — nothing disappears silently
    facts = [_fact(f"T{i:02d}", FACTOR, i + 1) for i in range(40)]
    facts += [_fact(f"H{i}", FACTOR, 10**6 * (i + 1)) for i in range(4)]
    d = FactorLens().build(facts)
    assert d["scope"]["fences"] == {"low": "-97", "high": "141"}
    assert d["outliers"]["count"] == 4
    assert [e["entity"] for e in d["outliers"]["high"]] == [
        "ticker:H3", "ticker:H2", "ticker:H1"]  # most extreme first
    assert d["outliers"]["low"] == []
    assert d["top"][0]["value"] == "40"  # inliers only


def test_factor_foreign_unit_never_shadows_factor_fact():
    # AA holds a valid "pure" factor fact AND a LATER foreign-unit fact with
    # the same concept — the foreign fact must not shadow the valid one
    entities = [Entity(ticker="AA"), Entity(ticker="BB"), Entity(ticker="CC")]
    facts = [_fact("AA", FACTOR, 5, end="2025-06-30"),
             _fact("AA", FACTOR, 999, unit="USD", end="2025-12-31"),
             _fact("BB", FACTOR, 6), _fact("CC", FACTOR, 7)]
    d = FactorLens().build(facts, entities)
    assert d["unclassified"] == {"entities": 0}
    assert [(e["entity"], e["value"]) for e in d["bottom"]] == [
        ("ticker:AA", "5"), ("ticker:BB", "6"), ("ticker:CC", "7")]
    assert d["bottom"][0]["period_end"] == "2025-06-30"
    assert d["bottom"][0]["citation"] == _fact("AA", FACTOR, 5,
                                               end="2025-06-30").cid


def test_factor_out_of_universe_entity_still_ranks():
    entities, facts = _universe()
    entities = [e for e in entities if e.ticker != "T09"]
    d = FactorLens().build(facts, entities)
    assert d["out_of_universe"] == 1
    assert d["top"][0]["entity"] == "ticker:T09"  # still in the cross-section
    assert d["deciles"][-1] == "100"


def test_factor_byte_determinism():
    entities, facts = _universe()
    d = FactorLens().build(facts, entities, max_citations_per_group=16)
    d2 = FactorLens().build(list(reversed(facts)), list(reversed(entities)),
                            max_citations_per_group=16)
    assert canonical_json(d) == canonical_json(d2)


@requires_pulse
def test_factor_lens_over_real_snapshot():
    from knitweb.core import crypto
    from knitweb.fabric.snapshot import web_snapshot
    from knitweb.fabric.web import Web
    from finfacts.model import FactSet
    from finknit.plugin import FinFieldKnitweb

    rows = [("AAA", 3674, 350000), ("BBB", 3674, 800000), ("CCC", 6022, 1200000)]
    kw = FinFieldKnitweb(crypto.generate_keypair()[0])
    web = Web()
    for ticker, sic, value in rows:
        fs = FactSet(entity=Entity(ticker=ticker))
        fs.add(_fact(ticker, "finfield:sic", sic))
        fs.add(_fact(ticker, FACTOR, value, scale=6))
        kw.weave_factset(fs, web)
    snap = web_snapshot(web)

    d = snapshot_digest(snap, FactorLens(top_n=2))
    assert d["state_root"] == snap["state_root"]
    assert d["rejected"] == 0
    assert d["deciles"][0] == "0.35" and d["deciles"][-1] == "1.2"
    assert len(d["deciles"]) == 11
    assert [(g["key"], g["entities"]) for g in d["groups"]] == [
        ("Finance, Insurance & Real Estate", 1), ("Manufacturing", 2)]
    assert [(e["entity"], e["value"]) for e in d["top"]] == [
        ("ticker:CCC", "1.2"), ("ticker:BBB", "0.8")]
    assert [(e["entity"], e["value"]) for e in d["bottom"]] == [
        ("ticker:AAA", "0.35"), ("ticker:BBB", "0.8")]

    # every citation — group, top and bottom — resolves in the @graph
    graph_ids = {node["id"] for node in snap["jsonld"]["@graph"]}
    for group in d["groups"]:
        assert group["citations"], group["key"]
        assert set(group["citations"]) <= graph_ids
    assert {e["citation"] for e in d["top"] + d["bottom"]} <= graph_ids

    # interpreting the same snapshot twice is byte-identical
    d2 = snapshot_digest(web_snapshot(web), FactorLens(top_n=2))
    assert canonical_json(d) == canonical_json(d2)
