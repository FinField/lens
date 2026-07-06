"""SIC classification: division range edges and major-group labels."""
from finlens.sectors import SIC_DIVISIONS, industry_of, sector_of

EDGES = [
    (100, "Agriculture, Forestry & Fishing"),
    (999, "Agriculture, Forestry & Fishing"),
    (1000, "Mining"),
    (1499, "Mining"),
    (1500, "Construction"),
    (1799, "Construction"),
    (2000, "Manufacturing"),
    (3999, "Manufacturing"),
    (4000, "Transportation & Public Utilities"),
    (4999, "Transportation & Public Utilities"),
    (5000, "Wholesale Trade"),
    (5199, "Wholesale Trade"),
    (5200, "Retail Trade"),
    (5999, "Retail Trade"),
    (6000, "Finance, Insurance & Real Estate"),
    (6799, "Finance, Insurance & Real Estate"),
    (7000, "Services"),
    (8999, "Services"),
    (9100, "Public Administration"),
    (9729, "Public Administration"),
]


def test_division_edges():
    for code, sector in EDGES:
        assert sector_of(code) == sector, code


def test_outside_and_between_ranges():
    for code in (0, 99, 1800, 1999, 6800, 6999, 9000, 9099, 9730, 9999):
        assert sector_of(code) == "unknown", code
        assert industry_of(code) == "unknown", code


def test_divisions_are_disjoint_and_sorted():
    bounds = [(lo, hi) for lo, hi, _ in SIC_DIVISIONS]
    assert bounds == sorted(bounds)
    for (_, hi), (lo, _) in zip(bounds, bounds[1:]):
        assert hi < lo


def test_major_group_labels():
    assert industry_of(3674) == "Electronic & Other Electric Equipment"
    assert industry_of(7372) == "Business Services"
    assert industry_of(6022) == "Depository Institutions"
    assert industry_of(2836) == "Chemicals & Allied Products"
    assert industry_of(1311) == "Oil & Gas Extraction"
    assert industry_of(4813) == "Communications"
    assert industry_of(6500) == "Real Estate"


def test_unlabeled_major_group_stays_visible():
    # 21xx (Tobacco) is classified Manufacturing but has no label yet:
    # it must group as "sic-21", never silently merge into "unknown".
    assert sector_of(2111) == "Manufacturing"
    assert industry_of(2111) == "sic-21"
