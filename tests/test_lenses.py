"""The four lenses over a synthetic 8-entity universe — medians hand-computed.

Universe: 3 sectors, 2 countries.

  ticker  country  sic   sector          revenue_ttm  margin(1e-6)  yoy(1e-6)
  AAA     US       3674  Manufacturing   100          50000         10000
  BBB     US       3674  Manufacturing   200          100000        20000
  CCC     US       3674  Manufacturing   300          150000        30000
  DDD     US       6022  Finance         1000         200000        -
  EEE     NL       6022  Finance         2000         250000        -
  FFF     NL       6022  Finance         3000         300000        -
  GGG     NL       7372  Services        10           400000        -
  HHH     NL       7372  Services        20           500000        -
"""
from finfacts.model import Entity, FinFact, Period, Source, canonical_json
from finlens.lenses import CountryLens, IndustryLens, MacroLens, SectorLens

SRC = Source(kind="test-fixture", ref="t1", fetched="2026-01-01")

ROWS = [
    ("AAA", "US", 3674, 100, 50000, 10000),
    ("BBB", "US", 3674, 200, 100000, 20000),
    ("CCC", "US", 3674, 300, 150000, 30000),
    ("DDD", "US", 6022, 1000, 200000, None),
    ("EEE", "NL", 6022, 2000, 250000, None),
    ("FFF", "NL", 6022, 3000, 300000, None),
    ("GGG", "NL", 7372, 10, 400000, None),
    ("HHH", "NL", 7372, 20, 500000, None),
]


def _fact(ticker, concept, value, scale=0, unit="USD", end="2025-12-31"):
    return FinFact(entity_id=f"ticker:{ticker}", concept=concept, value=value,
                   scale=scale, unit=unit, period=Period(end=end), source=SRC)


def _universe():
    entities, facts = [], []
    for ticker, country, sic, rev, margin, yoy in ROWS:
        entities.append(Entity(ticker=ticker, country=country))
        facts.append(_fact(ticker, "finfield:sic", sic, unit="pure"))
        facts.append(_fact(ticker, "finfield:revenue_ttm", rev))
        facts.append(_fact(ticker, "finfield:net_margin_ttm", margin, scale=6, unit="pure"))
        if yoy is not None:
            facts.append(_fact(ticker, "finfield:revenue_yoy", yoy, scale=6, unit="pure"))
    # a stale AAA revenue fact — the latest (2025-12-31) one must win
    facts.append(_fact("AAA", "finfield:revenue_ttm", 999999, end="2025-06-30"))
    return entities, facts


def _group(digest, key):
    return next(g for g in digest["groups"] if g["key"] == key)


def test_sector_lens_medians():
    entities, facts = _universe()
    d = SectorLens().build(facts, entities, max_citations_per_group=16)
    assert d["kind"] == "finfield-lens-digest"
    assert d["lens"] == "sector"
    assert "truncated" not in d
    assert d["unclassified"] == {"entities": 0}
    assert [g["key"] for g in d["groups"]] == [
        "Finance, Insurance & Real Estate", "Manufacturing", "Services"]

    manu = _group(d, "Manufacturing")
    assert manu["entities"] == 3
    assert manu["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "200", "p25": "100", "p75": "200", "n": 3}}
    assert manu["metrics"]["finfield:net_margin_ttm"]["pure"]["median"] == "0.1"
    assert manu["metrics"]["finfield:revenue_yoy"]["pure"]["median"] == "0.02"

    fin = _group(d, "Finance, Insurance & Real Estate")
    assert fin["entities"] == 3
    assert fin["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "2000", "p25": "1000", "p75": "2000", "n": 3}}
    assert fin["metrics"]["finfield:net_margin_ttm"]["pure"]["median"] == "0.25"
    assert "finfield:revenue_yoy" not in fin["metrics"]

    svc = _group(d, "Services")
    assert svc["entities"] == 2
    # even n -> lower interpolation everywhere
    assert svc["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "10", "p25": "10", "p75": "10", "n": 2}}
    assert svc["metrics"]["finfield:net_margin_ttm"]["pure"]["median"] == "0.4"


def test_latest_fact_wins_and_is_cited():
    entities, facts = _universe()
    d = SectorLens().build(facts, entities, max_citations_per_group=16)
    manu = _group(d, "Manufacturing")
    stale = _fact("AAA", "finfield:revenue_ttm", 999999, end="2025-06-30")
    fresh = _fact("AAA", "finfield:revenue_ttm", 100)
    assert stale.cid not in manu["citations"]
    assert fresh.cid in manu["citations"]
    # 3 revenue + 3 margin + 3 yoy latest facts + 3 classifying sic facts
    assert len(manu["citations"]) == 12


def test_classifying_sic_fact_is_cited():
    entities, facts = _universe()
    d = SectorLens().build(facts, entities, max_citations_per_group=16)
    manu = _group(d, "Manufacturing")
    for ticker in ("AAA", "BBB", "CCC"):
        sic = _fact(ticker, "finfield:sic", 3674, unit="pure")
        assert sic.cid in manu["citations"], ticker


def test_out_of_universe_entity_still_aggregates():
    # the reviewer's probe: CCC has a valid finfield:sic fact + revenue
    # facts but NO entity record — it must appear in its sic group (never
    # be dropped silently) and be counted in out_of_universe
    entities, facts = _universe()
    entities = [e for e in entities if e.ticker != "CCC"]
    d = SectorLens().build(facts, entities, max_citations_per_group=16)
    manu = _group(d, "Manufacturing")
    assert manu["entities"] == 3  # AAA, BBB and universe-less CCC
    assert manu["metrics"]["finfield:revenue_ttm"]["USD"]["median"] == "200"
    ccc_rev = _fact("CCC", "finfield:revenue_ttm", 300)
    assert ccc_rev.cid in manu["citations"]
    assert d["out_of_universe"] == 1
    assert d["unclassified"] == {"entities": 0}
    # with the full universe the counter is 0
    full = SectorLens().build(facts, _universe()[0])
    assert full["out_of_universe"] == 0


def test_industry_lens_groups():
    entities, facts = _universe()
    d = IndustryLens().build(facts, entities, max_citations_per_group=16)
    assert [(g["key"], g["entities"]) for g in d["groups"]] == [
        ("Business Services", 2),
        ("Depository Institutions", 3),
        ("Electronic & Other Electric Equipment", 3),
    ]
    assert _group(d, "Business Services")["metrics"][
        "finfield:revenue_ttm"]["USD"]["median"] == "10"


def test_country_lens_medians():
    entities, facts = _universe()
    d = CountryLens().build(facts, entities, max_citations_per_group=16)
    assert [(g["key"], g["entities"]) for g in d["groups"]] == [("NL", 4), ("US", 4)]
    us = _group(d, "US")   # revenues sorted: 100, 200, 300, 1000
    assert us["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "200", "p25": "100", "p75": "300", "n": 4}}
    assert us["metrics"]["finfield:net_margin_ttm"]["pure"]["median"] == "0.1"
    nl = _group(d, "NL")   # revenues sorted: 10, 20, 2000, 3000
    assert nl["metrics"]["finfield:revenue_ttm"] == {"USD": {
        "median": "20", "p25": "10", "p75": "2000", "n": 4}}
    assert nl["metrics"]["finfield:net_margin_ttm"]["pure"]["median"] == "0.3"


def test_unclassified_is_visible():
    entities = [Entity(ticker="III", country="US")]
    facts = [_fact("III", "finfield:revenue_ttm", 42)]
    d = SectorLens().build(facts, entities)  # no finfield:sic fact
    assert d["groups"] == []
    assert d["unclassified"] == {"entities": 1}
    assert d["out_of_universe"] == 0


def test_country_lens_missing_country_groups_as_qq():
    entities = [Entity(ticker="JJJ")]  # no country
    facts = [_fact("JJJ", "finfield:revenue_ttm", 7)]
    d = CountryLens().build(facts, entities)
    assert [g["key"] for g in d["groups"]] == ["??"]
    assert d["groups"][0]["entities"] == 1


def test_macro_lens_series_tail():
    months = ["2025-01-31", "2025-02-28", "2025-03-31",
              "2025-04-30", "2025-05-31", "2025-06-30"]
    entities = [Entity(ticker="US MACRO", asset="macro"),
                Entity(ticker="EU MACRO", asset="macro"),
                Entity(ticker="AAA", country="US")]  # equity -> unclassified
    facts = []
    for i, end in enumerate(months):  # US policy rate 4.00 .. 5.25 step 0.25
        facts.append(_fact("US MACRO", "finfield:policy_rate",
                           4000000 + 250000 * i, scale=6, unit="pure", end=end))
    facts.append(_fact("US MACRO", "finfield:cpi_yoy", 31000, scale=6,
                       unit="pure", end="2025-06-30"))
    facts.append(_fact("EU MACRO", "finfield:policy_rate", 2150000, scale=6,
                       unit="pure", end="2025-06-30"))
    facts.append(_fact("EU MACRO", "finfield:fx_usd", 1085000, scale=6,
                       unit="pure", end="2025-06-30"))

    d = MacroLens().build(facts, entities, tail=4)
    assert d["lens"] == "macro"
    assert [g["key"] for g in d["groups"]] == ["EU", "US"]
    assert d["unclassified"] == {"entities": 1}
    assert d["out_of_universe"] == 0
    assert d["truncated"] == {"dropped": 2}  # 6 US policy points, tail=4

    us = _group(d, "US")
    assert us["entities"] == 1
    points = us["series"]["finfield:policy_rate"]
    assert [p["period"] for p in points] == months[2:]
    assert [p["value"] for p in points] == ["4.5", "4.75", "5", "5.25"]
    assert all(p["citation"].startswith("ff1:") for p in points)
    assert us["series"]["finfield:cpi_yoy"] == [
        {"period": "2025-06-30", "value": "0.031",
         "citation": _fact("US MACRO", "finfield:cpi_yoy", 31000, scale=6,
                           unit="pure", end="2025-06-30").cid}]
    eu = _group(d, "EU")
    assert eu["series"]["finfield:policy_rate"][0]["value"] == "2.15"
    assert eu["series"]["finfield:fx_usd"][0]["value"] == "1.085"
    assert sorted({p["citation"] for s in eu["series"].values() for p in s}) \
        == eu["citations"]

    d2 = MacroLens().build(list(reversed(facts)), entities, tail=4)
    assert canonical_json(d) == canonical_json(d2)
