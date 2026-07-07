"""CompanyLens: bucket routing, derived chains, trailing history, snapshot.

Synthetic company "AIR US" (mirrors the real feed's first entity, which —
like every feed entity record — carries no country field, so the scope
country must come from the composite-ticker suffix).
"""
import pytest

from finfacts.model import Entity, FactSet, FinFact, Period, Source, canonical_json
from finlens.adapter import HAS_PULSE, snapshot_digest
from finlens.company import CONCEPT_BUCKETS, CompanyLens, is_derived_fact

requires_pulse = pytest.mark.skipif(not HAS_PULSE, reason="knitweb (pulse) not installed")

SRC = Source(kind="sec-companyfacts", ref="acc-1", fetched="2026-01-01")
DERIVED_SRC = Source(kind="finfield-derived", ref="finfield.smart.derive")
AIR = "ticker:AIR US"

QUARTERS = [  # 6 quarters -> history keeps the trailing 4
    ("2024-06-01", "2024-08-31", 100),
    ("2024-09-01", "2024-11-30", 200),
    ("2024-12-01", "2025-02-28", 300),
    ("2025-03-01", "2025-05-31", 400),
    ("2025-06-01", "2025-08-31", 500),
    ("2025-09-01", "2025-11-30", 600),
]


def _fact(concept, value, scale=0, unit="USD", end="2025-11-30", start=None,
          source=SRC, derived_from=(), entity_id=AIR):
    return FinFact(entity_id=entity_id, concept=concept, value=value,
                   scale=scale, unit=unit, period=Period(end=end, start=start),
                   source=source, derived_from=derived_from)


def _company_facts():
    quarters = [_fact("us-gaap:Revenues", v, start=s, end=e)
                for s, e, v in QUARTERS]
    flt = _fact("dei:EntityPublicFloat", 1000000)
    equity = _fact("us-gaap:StockholdersEquity", 500000)
    facts = quarters + [
        flt,
        equity,
        # a stale equity value — the fresh 2025-11-30 one must win
        _fact("us-gaap:StockholdersEquity", 999, end="2024-11-30"),
        _fact("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", 40000,
              start="2025-09-01"),
        _fact("us-gaap:EarningsPerShareBasic", 123, scale=2),
        _fact("us-gaap:NotInTheBucketMap", 5),
        # derived facts with their full provenance chains
        _fact("finfield:revenue_ttm", 1800, start="2024-12-01",
              source=DERIVED_SRC,
              derived_from=tuple(q.cid for q in quarters[-4:])),
        _fact("finfield:book_to_float_mcap", 500000, scale=6, unit="pure",
              source=DERIVED_SRC, derived_from=(equity.cid, flt.cid)),
        # a consensus fact routes to "derived" by source kind alone
        _fact("crowd:target_price", 7550, scale=2,
              source=Source(kind="finfield-consensus", ref="finknit.vote")),
        # another entity's fact — scoped out visibly, never silently
        _fact("us-gaap:Assets", 1, entity_id="ticker:OTH US"),
    ]
    return facts, quarters, flt, equity


def test_company_scope_with_ticker_country_fallback():
    facts, _, _, _ = _company_facts()
    entity = Entity(ticker="AIR US", name="AAR CORP", cik="1750")  # no country
    d = CompanyLens(AIR).build(facts, [entity])
    assert d["kind"] == "finfield-lens-digest"
    assert d["lens"] == "company"
    assert d["scope"] == {"entity": AIR, "name": "AAR CORP", "asset": "equity",
                          "cik": "1750", "country": "US"}
    assert d["groups"] == []  # this lens aggregates no population
    # entity records exist for neither OTH US nor... AIR US is supplied:
    assert d["out_of_universe"] == 1        # OTH US carries facts, no record
    assert d["unclassified"] == {"entities": 1}  # OTH US is out of scope


def test_company_bucket_routing_and_latest_wins():
    facts, quarters, flt, equity = _company_facts()
    d = CompanyLens(AIR).build(facts)
    assert set(d["facts"]) == {"shares", "income", "balance", "cashflow",
                               "per-share", "other"}
    assert d["facts"]["shares"] == [{
        "concept": "dei:EntityPublicFloat", "unit": "USD", "value": "1000000",
        "period_end": "2025-11-30", "source_ref": "acc-1",
        "citation": flt.cid}]
    # latest per (concept, unit): the fresh equity wins over the stale one
    assert d["facts"]["balance"] == [{
        "concept": "us-gaap:StockholdersEquity", "unit": "USD",
        "value": "500000", "period_end": "2025-11-30", "source_ref": "acc-1",
        "citation": equity.cid}]
    # income holds the latest quarter only (history has the trajectory)
    assert d["facts"]["income"] == [{
        "concept": "us-gaap:Revenues", "unit": "USD", "value": "600",
        "period_end": "2025-11-30", "source_ref": "acc-1",
        "citation": quarters[-1].cid}]
    assert d["facts"]["per-share"][0]["value"] == "1.23"
    assert d["facts"]["other"][0]["concept"] == "us-gaap:NotInTheBucketMap"
    assert "us-gaap:NotInTheBucketMap" not in CONCEPT_BUCKETS


def test_company_derived_chain_citations():
    facts, quarters, flt, equity = _company_facts()
    d = CompanyLens(AIR).build(facts)
    assert [e["concept"] for e in d["derived"]] == [
        "crowd:target_price",             # consensus source kind
        "finfield:book_to_float_mcap",    # finfield namespace
        "finfield:revenue_ttm",
    ]
    b2m = d["derived"][1]
    assert b2m["value"] == "0.5"
    assert b2m["period_end"] == "2025-11-30"  # stale-instant mixing is dated
    assert b2m["derived_from"] == [equity.cid, flt.cid]  # the chain
    assert b2m["citation"] == next(
        f for f in facts if f.concept == "finfield:book_to_float_mcap").cid
    ttm = d["derived"][2]
    assert ttm["derived_from"] == [q.cid for q in quarters[-4:]]
    assert ttm["value"] == "1800" and ttm["unit"] == "USD"
    # derived facts never leak into the statement buckets
    bucketed = {e["concept"] for entries in d["facts"].values() for e in entries}
    assert not any(is_derived_fact(f) for f in facts
                   if f.concept in bucketed)


def test_company_history_trailing_4_truncation_visible():
    facts, _, _, _ = _company_facts()
    d = CompanyLens(AIR).build(facts)
    hist = d["history"]["us-gaap:Revenues"]["USD"]
    assert hist == [{"period_end": e, "value": str(v)}
                    for _, e, v in QUARTERS[-4:]]
    # 6 quarters, tail 4 -> 2 points dropped, and the digest says so
    assert d["truncated"] == {"history": 2}
    # single-period duration concepts get no history entry (token-lean)
    assert "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment" not in d["history"]
    # instant concepts never do
    assert "us-gaap:StockholdersEquity" not in d["history"]
    # a longer tail keeps everything and drops the marker
    d6 = CompanyLens(AIR).build(facts, history_tail=6)
    assert len(d6["history"]["us-gaap:Revenues"]["USD"]) == 6
    assert "truncated" not in d6


def test_company_bucket_cap_truncation_visible():
    facts = [_fact("us-gaap:UnknownA", 1), _fact("us-gaap:UnknownB", 2)]
    d = CompanyLens(AIR).build(facts, max_per_bucket=1)
    assert [e["concept"] for e in d["facts"]["other"]] == ["us-gaap:UnknownA"]
    assert d["truncated"] == {"facts": 1}


def test_company_fact_entries_carry_fiscal_context():
    fiscal = FinFact(entity_id=AIR, concept="us-gaap:Revenues", value=700,
                     scale=0, unit="USD",
                     period=Period(end="2025-11-30", start="2025-09-01",
                                   fiscal_year=2026, fiscal_period="Q2"),
                     source=SRC)
    plain = _fact("us-gaap:Assets", 9000)  # period carries no fiscal context
    d = CompanyLens(AIR).build([fiscal, plain])
    income = d["facts"]["income"][0]
    assert income["fp"] == "Q2" and income["fy"] == 2026
    balance = d["facts"]["balance"][0]
    assert "fp" not in balance and "fy" not in balance


def test_is_derived_fact_classifies_by_provenance():
    scraped_sic = _fact("finfield:sic", 3728, unit="pure",
                        source=Source(kind="sec-submissions", ref="subs"))
    assert not is_derived_fact(scraped_sic)          # empty chain -> statement
    chained = _fact("finfield:revenue_ttm", 1800,
                    source=Source(kind="other-kind", ref="x"),
                    derived_from=("ff1:abc",))
    assert is_derived_fact(chained)                  # finfield:* + chain
    by_kind = _fact("crowd:target_price", 7550, scale=2,
                    source=Source(kind="finfield-consensus", ref="finknit.vote"))
    assert is_derived_fact(by_kind)                  # consensus source kind


def test_company_scraped_sic_routes_to_classification_bucket():
    sic = _fact("finfield:sic", 3728, unit="pure", scale=0,
                source=Source(kind="sec-submissions", ref="subs"))
    d = CompanyLens(AIR).build([sic])
    assert CONCEPT_BUCKETS["finfield:sic"] == "classification"
    assert d["facts"]["classification"] == [{
        "concept": "finfield:sic", "unit": "pure", "value": "3728",
        "period_end": "2025-11-30", "source_ref": "subs",
        "citation": sic.cid}]
    assert d["derived"] == []


def test_company_derived_cap_truncation_visible():
    derived = [_fact(f"finfield:ratio_{c}", 10 + i, source=DERIVED_SRC,
                     derived_from=("ff1:abc",))
               for i, c in enumerate("abc")]
    d = CompanyLens(AIR, max_derived=2).build(derived)
    # sorted (concept, unit) order; the cap keeps the first two, visibly
    assert [e["concept"] for e in d["derived"]] == [
        "finfield:ratio_a", "finfield:ratio_b"]
    assert d["truncated"] == {"derived": 1}
    uncapped = CompanyLens(AIR).build(derived)   # default cap 24
    assert len(uncapped["derived"]) == 3 and "truncated" not in uncapped


def test_company_history_concepts_cap_truncation_visible():
    facts = []
    for concept in ("us-gaap:CostOfRevenue", "us-gaap:Revenues"):
        for start, end, value in QUARTERS[:3]:
            facts.append(_fact(concept, value, start=start, end=end))
    d = CompanyLens(AIR, max_history_concepts=1).build(facts)
    # sorted (concept, unit): CostOfRevenue kept, Revenues' 3 points dropped
    assert list(d["history"]) == ["us-gaap:CostOfRevenue"]
    assert len(d["history"]["us-gaap:CostOfRevenue"]["USD"]) == 3
    assert d["truncated"] == {"history": 3}
    uncapped = CompanyLens(AIR).build(facts)     # default cap 12
    assert sorted(uncapped["history"]) == [
        "us-gaap:CostOfRevenue", "us-gaap:Revenues"]
    assert "truncated" not in uncapped


def test_company_byte_determinism():
    facts, _, _, _ = _company_facts()
    entity = Entity(ticker="AIR US", name="AAR CORP", cik="1750")
    d = CompanyLens(AIR).build(facts, [entity])
    d2 = CompanyLens(AIR).build(list(reversed(facts)), [entity])
    assert canonical_json(d) == canonical_json(d2)


@requires_pulse
def test_company_lens_over_real_snapshot():
    from knitweb.core import crypto
    from knitweb.fabric.snapshot import web_snapshot
    from knitweb.fabric.web import Web
    from finknit.plugin import FinFieldKnitweb

    facts, quarters, flt, equity = _company_facts()
    base = [f for f in facts
            if f.entity_id == AIR and not is_derived_fact(f)]
    derived = [f for f in facts
               if f.entity_id == AIR and f.source.kind == "finfield-derived"]
    kw = FinFieldKnitweb(crypto.generate_keypair()[0])
    web = Web()
    fs = FactSet(entity=Entity(ticker="AIR US", name="AAR CORP", cik="1750"),
                 facts=base)
    kw.weave_factset(fs, web, derived=derived)
    other = FactSet(entity=Entity(ticker="OTH US"))
    other.add(_fact("us-gaap:Assets", 1, entity_id="ticker:OTH US"))
    kw.weave_factset(other, web)
    snap = web_snapshot(web)

    d = snapshot_digest(snap, CompanyLens(AIR))
    assert d["state_root"] == snap["state_root"]
    assert d["rejected"] == 0
    assert d["out_of_universe"] == 0             # both entity records woven
    assert d["unclassified"] == {"entities": 1}  # OTH US scoped out, visibly

    # scope: metadata + ticker-suffix country + the entity record's own CID
    graph_ids = {node["id"] for node in snap["jsonld"]["@graph"]}
    assert d["scope"]["name"] == "AAR CORP"
    assert d["scope"]["country"] == "US"  # record carries no country field
    assert d["scope"]["citation"] in graph_ids

    # every citation and every derived_from link resolves in the @graph
    entries = [e for entries in d["facts"].values() for e in entries]
    assert {e["citation"] for e in entries} <= graph_ids
    for entry in d["derived"]:
        assert entry["citation"] in graph_ids
        assert entry["derived_from"], entry["concept"]
        assert set(entry["derived_from"]) <= graph_ids
    assert [e["concept"] for e in d["derived"]] == [
        "finfield:book_to_float_mcap", "finfield:revenue_ttm"]

    # history rides along in the snapshot path too
    assert [p["value"] for p in d["history"]["us-gaap:Revenues"]["USD"]] == [
        "300", "400", "500", "600"]
    assert d["truncated"] == {"history": 2}

    # interpreting the same snapshot twice is byte-identical
    d2 = snapshot_digest(web_snapshot(web), CompanyLens(AIR))
    assert canonical_json(d) == canonical_json(d2)
