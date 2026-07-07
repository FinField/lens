"""REAL pulse integration: weave facts, snapshot, interpret, verify.

Runs against the actual knitweb runtime: it weaves a small universe with
FinFieldKnitweb, takes a ``web_snapshot``, interprets it through the
lenses, and checks every Lens-contract obligation — 64-hex ``state_root``,
citations resolvable in the snapshot's ``@graph``, byte-identical repeated
interpretation, and a Web that is unchanged after being read.
"""
import json

import pytest

from finfacts.model import Entity, FactSet, FinFact, Period, Source, canonical_json
from finlens.adapter import HAS_PULSE, snapshot_digest, to_atoms, to_message_payload
from finlens.lenses import CountryLens, IndustryLens, MacroLens, SectorLens

requires_pulse = pytest.mark.skipif(not HAS_PULSE, reason="knitweb (pulse) not installed")

SRC = Source(kind="test-fixture", ref="t1", fetched="2026-01-01")
HEX = set("0123456789abcdef")

ROWS = [  # ticker, country, sic, revenue_ttm
    ("AAA", "US", 3674, 100),
    ("BBB", "US", 3674, 200),
    ("CCC", "NL", 6022, 3000),
]


def _fact(ticker, concept, value, scale=0, unit="USD"):
    return FinFact(entity_id=f"ticker:{ticker}", concept=concept, value=value,
                   scale=scale, unit=unit, period=Period(end="2025-12-31"),
                   source=SRC)


def _weave_universe():
    from knitweb.core import crypto
    from knitweb.fabric.web import Web
    from finknit.plugin import FinFieldKnitweb

    kw = FinFieldKnitweb(crypto.generate_keypair()[0])
    web = Web()
    for ticker, country, sic, revenue in ROWS:
        fs = FactSet(entity=Entity(ticker=ticker, country=country))
        fs.add(_fact(ticker, "finfield:sic", sic, unit="pure"))
        fs.add(_fact(ticker, "finfield:revenue_ttm", revenue))
        kw.weave_factset(fs, web)
    # a malformed finfact-record (missing value/period) — must be counted
    # as rejected, never silently become data
    web.weave({"kind": "finfact-record", "entity": "ticker:BAD US"})
    return web


@requires_pulse
def test_sector_lens_over_real_snapshot():
    from knitweb.fabric.snapshot import web_snapshot

    web = _weave_universe()
    snap = web_snapshot(web)
    root_before = snap["state_root"]

    d = snapshot_digest(snap, SectorLens())

    # state_root: 64 hex, bound to the snapshot
    assert d["state_root"] == root_before
    assert len(d["state_root"]) == 64 and set(d["state_root"]) <= HEX

    # the malformed record is visible, not swallowed
    assert d["rejected"] == 1

    # groups: AAA+BBB manufacturing, CCC finance; entities from the
    # snapshot's own finfield-entity records
    assert [(g["key"], g["entities"]) for g in d["groups"]] == [
        ("Finance, Insurance & Real Estate", 1), ("Manufacturing", 2)]
    manu = d["groups"][1]
    assert manu["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "100", "p25": "100", "p75": "100", "n": 2}}
    assert d["out_of_universe"] == 0

    # every citation is a knit-CID present in the snapshot's @graph
    graph_ids = {node["id"] for node in snap["jsonld"]["@graph"]}
    for group in d["groups"]:
        assert group["citations"], group["key"]
        assert set(group["citations"]) <= graph_ids

    # interpreting the same snapshot twice is byte-identical
    d2 = snapshot_digest(web_snapshot(web), SectorLens())
    assert canonical_json(d) == canonical_json(d2)

    # the Web a lens read is byte-identical to the Web after it read
    snap_after = web_snapshot(web)
    assert snap_after["state_root"] == root_before
    assert canonical_json(snap_after["jsonld"]) == canonical_json(snap["jsonld"])


@requires_pulse
def test_country_lens_over_real_snapshot():
    from knitweb.fabric.snapshot import web_snapshot

    web = _weave_universe()
    snap = web_snapshot(web)
    d = snapshot_digest(snap, CountryLens())

    assert [(g["key"], g["entities"]) for g in d["groups"]] == [("NL", 1), ("US", 2)]
    assert d["state_root"] == snap["state_root"]
    graph_ids = {node["id"] for node in snap["jsonld"]["@graph"]}
    for group in d["groups"]:
        assert set(group["citations"]) <= graph_ids

    # the finfield-entity record that classified CCC into NL is cited
    ccc_entity_cid = next(
        node["id"] for node in snap["jsonld"]["@graph"]
        if (node.get("record") or {}).get("kind") == "finfield-entity"
        and node["record"].get("ticker") == "CCC")
    assert ccc_entity_cid in d["groups"][0]["citations"]

    # caps pass through to the lens
    capped = snapshot_digest(snap, CountryLens(), max_groups=1)
    assert len(capped["groups"]) == 1
    assert capped["truncated"] == {"groups": 1}


@requires_pulse
def test_industry_lens_over_real_snapshot():
    from knitweb.fabric.snapshot import web_snapshot

    web = _weave_universe()
    snap = web_snapshot(web)
    d = snapshot_digest(snap, IndustryLens())

    assert d["state_root"] == snap["state_root"]
    assert d["rejected"] == 1
    assert [(g["key"], g["entities"]) for g in d["groups"]] == [
        ("Depository Institutions", 1),
        ("Electronic & Other Electric Equipment", 2)]
    elec = d["groups"][1]
    assert elec["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "100", "p25": "100", "p75": "100", "n": 2}}
    graph_ids = {node["id"] for node in snap["jsonld"]["@graph"]}
    for group in d["groups"]:
        assert group["citations"], group["key"]
        assert set(group["citations"]) <= graph_ids


@requires_pulse
def test_macro_lens_over_real_snapshot():
    from knitweb.core import crypto
    from knitweb.fabric.snapshot import web_snapshot
    from knitweb.fabric.web import Web
    from finknit.plugin import FinFieldKnitweb

    months = ["2025-03-31", "2025-04-30", "2025-05-31", "2025-06-30"]
    kw = FinFieldKnitweb(crypto.generate_keypair()[0])
    web = Web()
    fs = FactSet(entity=Entity(ticker="US MACRO", asset="macro"))
    for i, end in enumerate(months):  # 4.00, 4.25, 4.50, 4.75
        fs.add(FinFact(entity_id="ticker:US MACRO",
                       concept="finfield:policy_rate",
                       value=4000000 + 250000 * i, scale=6, unit="pure",
                       period=Period(end=end), source=SRC))
    kw.weave_factset(fs, web)
    snap = web_snapshot(web)

    d = snapshot_digest(snap, MacroLens(), tail=2)  # tail kwarg honored
    assert d["state_root"] == snap["state_root"]
    assert d["unclassified"] == {"entities": 0}
    assert d["out_of_universe"] == 0
    assert d["truncated"] == {"series": 2}  # 4 points, tail=2

    assert [g["key"] for g in d["groups"]] == ["US"]
    us = d["groups"][0]
    points = us["series"]["finfield:policy_rate"]
    assert [(p["period"], p["value"]) for p in points] == [
        ("2025-05-31", "4.5"), ("2025-06-30", "4.75")]

    graph_ids = {node["id"] for node in snap["jsonld"]["@graph"]}
    assert {p["citation"] for p in points} <= graph_ids
    # the finfield-entity record (asset="macro") that classified the
    # region is cited alongside the series points
    entity_cid = next(
        node["id"] for node in snap["jsonld"]["@graph"]
        if (node.get("record") or {}).get("kind") == "finfield-entity")
    assert entity_cid in us["citations"]
    assert set(us["citations"]) <= graph_ids


@requires_pulse
def test_malformed_entity_record_always_rejected():
    from knitweb.fabric.snapshot import web_snapshot

    web = _weave_universe()
    web.weave({"kind": "finfield-entity", "note": "no ticker"})
    snap = web_snapshot(web)

    # reconstructed universe: malformed fact record + malformed entity record
    assert snapshot_digest(snap, SectorLens())["rejected"] == 2
    # supplied universe: the decode attempt happens regardless — same tally
    entities = [Entity(ticker=t, country=c) for t, c, _, _ in ROWS]
    d = snapshot_digest(snap, SectorLens(), entities=entities)
    assert d["rejected"] == 2


@requires_pulse
def test_message_payload_and_atoms():
    from knitweb.fabric.snapshot import web_snapshot
    from knitweb.lens.atom import ExpressionAtom

    web = _weave_universe()
    snap = web_snapshot(web)
    d = snapshot_digest(snap, SectorLens())

    payload = to_message_payload("pls1node", "finfield/lens/sector", d)
    assert payload["kind"] == "finfield-lens-digest"
    assert payload["sender"] == "pls1node"
    assert payload["topic"] == "finfield/lens/sector"
    assert payload["lens"] == "sector"
    assert payload["group_count"] == 2
    assert payload["state_root"] == snap["state_root"]
    assert payload["conforming"] is True  # snapshot-bound answer
    assert json.loads(payload["content"]) == d

    atoms = to_atoms(d)
    assert len(atoms) == 1 + len(d["groups"])
    assert all(isinstance(a, ExpressionAtom) for a in atoms)


def test_message_payload_flags_bare_digest_nonconforming():
    # no snapshot, no state_root: per LENS_RLM_CONTRACT.md that answer is
    # non-conforming, and the payload must say so — no pulse required
    facts = [_fact("AAA", "finfield:sic", 3674, unit="pure"),
             _fact("AAA", "finfield:revenue_ttm", 100)]
    d = SectorLens().build(facts, [Entity(ticker="AAA", country="US")])
    assert d["state_root"] is None

    payload = to_message_payload("pls1node", "finfield/lens/sector", d)
    assert payload["state_root"] is None
    assert payload["conforming"] is False
    assert json.loads(payload["content"]) == d
