"""Public-domain SIC classification: division sectors + major-group industries.

FinField deliberately carries only open identifiers, so its sector/industry
lensing rests on the U.S. Standard Industrial Classification — public
domain, and exactly what SEC EDGAR publishes per registrant. GICS and other
licensed schemes are absent by design.

Facts convention: a company's classification is one fact per entity with
concept ``finfield:sic``, integer value (the 4-digit SIC code), scale 0,
unit ``"pure"`` — scraped from EDGAR by the finscrapers backlog.
"""
from __future__ import annotations

SIC_CONCEPT = "finfield:sic"

# The standard SIC divisions as (low, high, sector) — inclusive code ranges.
SIC_DIVISIONS: tuple = (
    (100, 999, "Agriculture, Forestry & Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation & Public Utilities"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance & Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9729, "Public Administration"),
)

# Major-group (2-digit) industry labels for the groups that dominate listed
# universes. A classified code outside this table still groups visibly (see
# industry_of), so the table can grow without breaking older digests.
SIC_MAJOR_GROUPS: dict = {
    1: "Agricultural Production - Crops",
    10: "Metal Mining",
    13: "Oil & Gas Extraction",
    15: "General Building Contractors",
    20: "Food & Kindred Products",
    26: "Paper & Allied Products",
    28: "Chemicals & Allied Products",
    29: "Petroleum Refining",
    33: "Primary Metal Industries",
    35: "Industrial Machinery & Equipment",
    36: "Electronic & Other Electric Equipment",
    37: "Transportation Equipment",
    38: "Instruments & Related Products",
    48: "Communications",
    49: "Electric, Gas & Sanitary Services",
    50: "Wholesale Trade - Durable Goods",
    53: "General Merchandise Stores",
    58: "Eating & Drinking Places",
    60: "Depository Institutions",
    62: "Security & Commodity Brokers",
    63: "Insurance Carriers",
    65: "Real Estate",
    67: "Holding & Other Investment Offices",
    70: "Hotels & Other Lodging Places",
    73: "Business Services",
    80: "Health Services",
    87: "Engineering & Management Services",
}


def sector_of(sic: int) -> str:
    """The SIC division a code falls in; ``"unknown"`` outside the ranges."""
    for low, high, sector in SIC_DIVISIONS:
        if low <= sic <= high:
            return sector
    return "unknown"


def industry_of(sic: int) -> str:
    """The 2-digit major-group industry label for a SIC code.

    ``"unknown"`` outside the division ranges; a valid code whose major
    group has no label yet groups as ``"sic-NN"`` — classified codes are
    never silently merged into "unknown".
    """
    if sector_of(sic) == "unknown":
        return "unknown"
    major_group = sic // 100
    return SIC_MAJOR_GROUPS.get(major_group, f"sic-{major_group:02d}")
