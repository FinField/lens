"""The classification analyst lenses: sector, industry, country, macro, factor.

Each lens is a pure read model over a bag of facts: it groups entities,
picks the latest fact per (entity, concept) with a deterministic tie-break,
aggregates by the exact quantile rule in :mod:`finlens.digest`, and cites
the CIDs of every fact it leaned on. A lens holds no state and never
mutates its inputs — interpreting the same facts twice yields the
byte-identical digest.

Facts arrive as ``FinFact`` objects or ``(FinFact, citation_cid)`` pairs;
the snapshot adapter supplies the latter so citations resolve inside the
snapshot's ``@graph``. Entities that a lens cannot classify are counted in
the digest's ``unclassified`` — never dropped silently. The entity universe
is always the UNION of the supplied entities and the entities seen in the
facts: a fact entity without an entity record still aggregates (it lands in
the visible catch-all where classification needs metadata) and is counted
in the digest's ``out_of_universe``. The fact that decided an entity's
group key is cited by that group; where the classifier is an entity record
rather than a fact, the optional ``entity_citations`` mapping
(``entity_id -> citation_cid``) supplies its citation.
"""
from __future__ import annotations

from typing import Iterable, Optional

from finfacts.model import Entity, FinFact

from .digest import (
    as_pairs,
    build_digest,
    digest_envelope,
    quantile_lower,
    render_scaled,
)
from .sectors import SIC_CONCEPT, industry_of, sector_of

# The comparable per-entity metrics the classification lenses aggregate.
AGG_CONCEPTS = (
    "finfield:revenue_ttm",
    "finfield:net_margin_ttm",
    "finfield:revenue_yoy",
)

# Macro convention: pseudo-entities "XX MACRO" (asset="macro") publish these
# region-level series as scale-6 "pure" facts.
MACRO_SUFFIX = " MACRO"
MACRO_CONCEPTS = (
    "finfield:policy_rate",
    "finfield:cpi_yoy",
    "finfield:fx_usd",
)


def ticker_country(ticker: str) -> Optional[str]:
    """Country code from a composite ticker: ``"AIR US" -> "US"``.

    Real-feed ``finfield-entity`` records carry no country field, but the
    composite ticker does: its second token is the two-letter exchange
    country code. Deterministic and strict — anything that is not exactly
    two space-separated tokens with a two-ASCII-uppercase-letter suffix
    (one-token tickers like ``"BTC"``, pseudo-tickers like ``"US MACRO"``)
    yields ``None``.
    """
    parts = ticker.split(" ")
    if (len(parts) == 2 and len(parts[1]) == 2 and parts[1].isascii()
            and parts[1].isalpha() and parts[1].isupper()):
        return parts[1]
    return None


def latest_by_entity_concept(pairs: Iterable[tuple]) -> dict:
    """Latest ``(fact, citation)`` per (entity_id, concept).

    Deterministic tie-break: the maximum of ``(period.end, fact.cid,
    citation)`` wins, so every node picks the same fact regardless of
    input order.
    """
    latest: dict = {}
    for fact, citation in pairs:
        key = (fact.entity_id, fact.concept)
        rank = (fact.period.end, fact.cid, citation)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, fact, citation)
    return {key: (fact, citation) for key, (_, fact, citation) in latest.items()}


class _GroupedLens:
    """Shared flow for the sector/industry/country lenses."""

    name = ""

    def scope(self) -> dict:
        raise NotImplementedError

    def _classify(self, entity_id: str, entity: Optional[Entity], latest: dict,
                  entity_citations: dict) -> tuple:
        """``(group_key, classifying_citation)``; key ``None`` = unclassified.

        The citation is the CID of whatever decided the key — a
        classification fact or an entity record — so the group can cite
        its own grouping provenance; ``None`` when nothing decided it.
        """
        raise NotImplementedError

    def build(self, facts: Iterable, entities: Optional[Iterable[Entity]] = None,
              max_groups: int = 32, max_citations_per_group: int = 8,
              entity_citations: Optional[dict] = None) -> dict:
        pairs = as_pairs(facts)
        latest = latest_by_entity_concept(pairs)
        entity_map = {e.entity_id: e for e in entities} if entities is not None else {}
        entity_citations = dict(entity_citations) if entity_citations else {}
        fact_entities = {fact.entity_id for fact, _ in pairs}
        # UNION universe: a fact entity without an entity record still
        # aggregates — and is counted, never silently dropped.
        universe = sorted(set(entity_map) | fact_entities)
        out_of_universe = (len(fact_entities - set(entity_map))
                           if entities is not None else 0)

        grouped: dict = {}
        group_entities: dict = {}
        group_citations: dict = {}
        unclassified = 0
        for entity_id in universe:
            key, classifier = self._classify(
                entity_id, entity_map.get(entity_id), latest, entity_citations)
            if key is None:
                unclassified += 1
                continue
            group_entities[key] = group_entities.get(key, 0) + 1
            if classifier is not None:
                group_citations.setdefault(key, set()).add(classifier)
            rows = grouped.setdefault(key, [])
            for concept in AGG_CONCEPTS:
                hit = latest.get((entity_id, concept))
                if hit is not None:
                    rows.append(hit)

        return build_digest(
            self.name, self.scope(), grouped,
            max_groups=max_groups,
            max_citations_per_group=max_citations_per_group,
            group_entities=group_entities,
            group_citations=group_citations,
            unclassified={"entities": unclassified},
            out_of_universe=out_of_universe,
        )


class SectorLens(_GroupedLens):
    """Group entities into SIC division sectors via their finfield:sic fact."""

    name = "sector"

    def scope(self) -> dict:
        return {"group_by": "sector", "classification": "sic-division",
                "concepts": list(AGG_CONCEPTS)}

    def _classify(self, entity_id, entity, latest, entity_citations):
        hit = latest.get((entity_id, SIC_CONCEPT))
        if hit is None:
            return None, None
        return sector_of(hit[0].value), hit[1]


class IndustryLens(_GroupedLens):
    """Group entities into SIC major-group industries."""

    name = "industry"

    def scope(self) -> dict:
        return {"group_by": "industry", "classification": "sic-major-group",
                "concepts": list(AGG_CONCEPTS)}

    def _classify(self, entity_id, entity, latest, entity_citations):
        hit = latest.get((entity_id, SIC_CONCEPT))
        if hit is None:
            return None, None
        return industry_of(hit[0].value), hit[1]


class CountryLens(_GroupedLens):
    """Group entities by Entity.country, with the composite-ticker fallback.

    When ``Entity.country`` is empty — or there is no entity record at all —
    the composite-ticker suffix decides the group (``"AIR US" -> "US"``, see
    :func:`ticker_country`); the real feed's entity records carry no country
    field, so without the fallback every feed entity would land in ``"??"``.
    Tickers without the two-part shape group visibly as ``"??"``.
    """

    name = "country"

    def scope(self) -> dict:
        return {"group_by": "country", "concepts": list(AGG_CONCEPTS)}

    def _classify(self, entity_id, entity, latest, entity_citations):
        if entity is not None and entity.country:
            return entity.country, entity_citations.get(entity_id)
        suffix = ticker_country(entity_id.split(":", 1)[-1])
        return (suffix if suffix is not None else "??",
                entity_citations.get(entity_id))


class MacroLens:
    """Per-region macro time series tails from "XX MACRO" pseudo-entities.

    A macro digest group carries, per concept, the last ``tail`` points of
    the series — each point a ``{"period", "value", "citation"}`` triple —
    so an analyst LLM sees level *and* trajectory, with every point
    independently verifiable. Points dropped by the tail cap are counted
    in ``truncated["series"]`` (groups and citations under their own kinds).
    """

    name = "macro"

    def scope(self) -> dict:
        return {"group_by": "region", "convention": "XX MACRO",
                "concepts": list(MACRO_CONCEPTS)}

    @staticmethod
    def region_of(entity_id: str) -> Optional[str]:
        ticker = entity_id.split(":", 1)[-1]
        if ticker.endswith(MACRO_SUFFIX) and len(ticker) > len(MACRO_SUFFIX):
            return ticker[: -len(MACRO_SUFFIX)]
        return None

    def build(self, facts: Iterable, entities: Optional[Iterable[Entity]] = None,
              tail: int = 8, max_groups: int = 32,
              max_citations_per_group: int = 8,
              entity_citations: Optional[dict] = None) -> dict:
        pairs = as_pairs(facts)
        entity_map = {e.entity_id: e for e in entities} if entities is not None else {}
        entity_citations = dict(entity_citations) if entity_citations else {}
        fact_entities = {fact.entity_id for fact, _ in pairs}
        # UNION universe, mirroring _GroupedLens: fact entities without an
        # entity record still aggregate, and are counted.
        universe = sorted(set(entity_map) | fact_entities)
        out_of_universe = (len(fact_entities - set(entity_map))
                           if entities is not None else 0)

        regions: dict = {}
        unclassified = 0
        for entity_id in universe:
            entity = entity_map.get(entity_id)
            region = self.region_of(entity_id)
            if region is None or (entity is not None and entity.asset != "macro"):
                unclassified += 1
                continue
            regions.setdefault(region, set()).add(entity_id)

        # One point per (region, concept, period.end); ties resolve to the
        # maximum (fact.cid, citation) so replays converge.
        series: dict = {}
        by_region = {entity_id: region for region, ids in regions.items()
                     for entity_id in ids}
        for fact, citation in pairs:
            region = by_region.get(fact.entity_id)
            if region is None or fact.concept not in MACRO_CONCEPTS:
                continue
            key = (region, fact.concept, fact.period.end)
            rank = (fact.cid, citation)
            if key not in series or rank > series[key][0]:
                series[key] = (rank, fact, citation)

        dropped_groups = dropped_series = dropped_citations = 0
        keys = sorted(regions)
        if len(keys) > max_groups:
            dropped_groups += len(keys) - max_groups
            keys = keys[:max_groups]

        groups = []
        for region in keys:
            concept_series: dict = {}
            # the entity records that classified the region are citations too
            citations: set = {entity_citations[entity_id]
                              for entity_id in regions[region]
                              if entity_id in entity_citations}
            for concept in MACRO_CONCEPTS:
                ends = sorted(end for (r, c, end) in series
                              if r == region and c == concept)
                if len(ends) > tail:
                    dropped_series += len(ends) - tail
                    ends = ends[-tail:]
                points = []
                for end in ends:
                    _, fact, citation = series[(region, concept, end)]
                    points.append({"period": end,
                                   "value": render_scaled(fact.value, fact.scale),
                                   "citation": citation})
                    citations.add(citation)
                if points:
                    concept_series[concept] = points
            cited = sorted(citations)
            if len(cited) > max_citations_per_group:
                dropped_citations += len(cited) - max_citations_per_group
                cited = cited[:max_citations_per_group]
            groups.append({"key": region, "entities": len(regions[region]),
                           "series": concept_series, "citations": cited})

        return digest_envelope(self.name, self.scope(), groups,
                               unclassified={"entities": unclassified},
                               truncated={"groups": dropped_groups,
                                          "series": dropped_series,
                                          "citations": dropped_citations},
                               out_of_universe=out_of_universe)


class FactorLens:
    """Cross-sectional view of one factor concept — the screen an analyst
    LLM actually needs for "is this cheap?" questions.

    The cross-section is the latest factor fact per entity — facts are
    filtered to ``factor_unit`` *before* latest-selection (a later fact in
    a foreign unit never shadows a valid factor fact; a factor never mixes
    units), then the same ``(period.end, cid)`` tie-break as the grouped
    lenses picks one fact per entity. On top of the standard envelope the
    digest carries:

    - ``"deciles"``: the 11 boundary values (min, d1..d9, max) of the
      cross-section — exact lower-interpolation quantiles at the finest
      common scale, rendered exact; ``[]`` when the cross-section is empty;
    - ``"groups"``: per SIC-division sector (via each entity's
      ``finfield:sic`` fact; entities with a factor value but no sic fact
      group visibly under ``"unclassified"``) the standard metric block for
      the factor concept, entity count, and citations — the factor-fact
      CIDs plus the classifying sic CIDs, capped with visible truncation;
    - ``"top"`` / ``"bottom"``: the ``top_n`` entities by factor value as
      ``{"entity", "value", "period_end", "citation"}``, selected only from
      values within the Tukey-style fences (below). Ordering is fully
      deterministic: ``bottom`` ascends by ``(value, entity_id)``; ``top``
      descends by value with ties broken by ascending entity_id;
    - ``"outliers"``: values outside the fences never rank in top/bottom
      but never disappear either — ``{"count": n, "high": [entries, max 3
      most extreme], "low": [entries, max 3 most extreme]}``, entries
      shaped like top/bottom entries.

    Degenerate outliers are fenced with exact integer math at the common
    scale: ``lo = d1 - 3*(d9 - d1)``, ``hi = d9 + 3*(d9 - d1)`` over the
    cross-section's own deciles; the rendered fences ride in
    ``scope["fences"]``. Scope also states the factor's ``orientation``
    (which end is "good") and a ``staleness_note`` — ratio inputs may
    differ by up to 400 days under the derive guard, and every ranked
    entry carries its factor fact's ``period_end`` so mixing is visible.

    Entities without a usable factor value (no factor fact, or only
    foreign-unit ones) cannot enter the cross-section and are counted in
    the digest's ``unclassified`` — distinct from the visible
    ``"unclassified"`` sector group, which holds entities *with* a factor
    value but no sic.
    """

    name = "factor"

    def __init__(self, factor_concept: str = "finfield:book_to_float_mcap",
                 factor_unit: str = "pure", top_n: int = 5,
                 orientation: str = ("higher = cheaper (book value per unit "
                                     "of free-float market cap)")) -> None:
        self.factor_concept = factor_concept
        self.factor_unit = factor_unit
        self.top_n = top_n
        self.orientation = orientation

    def scope(self) -> dict:
        return {"factor": self.factor_concept, "unit": self.factor_unit,
                "top_n": self.top_n, "orientation": self.orientation,
                "staleness_note": ("ratio inputs may differ by up to "
                                   "400 days (derive guard)")}

    def build(self, facts: Iterable, entities: Optional[Iterable[Entity]] = None,
              max_groups: int = 32, max_citations_per_group: int = 8,
              entity_citations: Optional[dict] = None) -> dict:
        pairs = as_pairs(facts)
        latest = latest_by_entity_concept(pairs)
        # factor facts filter to factor_unit BEFORE latest-selection, so a
        # later foreign-unit fact never shadows a valid factor fact
        factor_latest = latest_by_entity_concept(
            (fact, citation) for fact, citation in pairs
            if fact.concept == self.factor_concept
            and fact.unit == self.factor_unit)
        entity_map = {e.entity_id: e for e in entities} if entities is not None else {}
        fact_entities = {fact.entity_id for fact, _ in pairs}
        # UNION universe, mirroring _GroupedLens: a factor fact without an
        # entity record still enters the cross-section, and is counted.
        universe = sorted(set(entity_map) | fact_entities)
        out_of_universe = (len(fact_entities - set(entity_map))
                           if entities is not None else 0)

        cross: list = []  # one (fact, citation) per entity in the cross-section
        grouped: dict = {}
        group_entities: dict = {}
        group_citations: dict = {}
        unclassified = 0
        for entity_id in universe:
            hit = factor_latest.get((entity_id, self.factor_concept))
            if hit is None:
                unclassified += 1
                continue
            cross.append(hit)
            sic_hit = latest.get((entity_id, SIC_CONCEPT))
            if sic_hit is None:
                key = "unclassified"
            else:
                key = sector_of(sic_hit[0].value)
                group_citations.setdefault(key, set()).add(sic_hit[1])
            group_entities[key] = group_entities.get(key, 0) + 1
            grouped.setdefault(key, []).append(hit)

        digest = build_digest(
            self.name, self.scope(), grouped,
            max_groups=max_groups,
            max_citations_per_group=max_citations_per_group,
            group_entities=group_entities,
            group_citations=group_citations,
            unclassified={"entities": unclassified},
            out_of_universe=out_of_universe,
        )

        if cross:
            common = max(fact.scale for fact, _ in cross)
            rows = sorted((fact.value * 10 ** (common - fact.scale),
                           fact.entity_id, citation, fact.period.end)
                          for fact, citation in cross)
            values = [row[0] for row in rows]
            digest["deciles"] = [
                render_scaled(quantile_lower(values, i, 10), common)
                for i in range(11)]

            # Tukey-style fences on the cross-section's own deciles — exact
            # integer math at the common scale. top/bottom rank inliers
            # only; everything outside goes to the visible outliers section.
            d1 = quantile_lower(values, 1, 10)
            d9 = quantile_lower(values, 9, 10)
            lo = d1 - 3 * (d9 - d1)
            hi = d9 + 3 * (d9 - d1)
            digest["scope"]["fences"] = {"low": render_scaled(lo, common),
                                         "high": render_scaled(hi, common)}

            def entry(row: tuple) -> dict:
                value, entity_id, citation, period_end = row
                return {"entity": entity_id,
                        "value": render_scaled(value, common),
                        "period_end": period_end,
                        "citation": citation}

            inliers = [r for r in rows if lo <= r[0] <= hi]
            low = [r for r in rows if r[0] < lo]    # ascending: most extreme first
            high = sorted((r for r in rows if r[0] > hi),
                          key=lambda r: (-r[0], r[1]))  # most extreme first
            digest["bottom"] = [entry(r) for r in inliers[:self.top_n]]
            digest["top"] = [entry(r) for r in
                             sorted(inliers, key=lambda r: (-r[0], r[1]))
                             [:self.top_n]]
            digest["outliers"] = {"count": len(low) + len(high),
                                  "high": [entry(r) for r in high[:3]],
                                  "low": [entry(r) for r in low[:3]]}
        else:
            digest["deciles"] = []
            digest["bottom"] = []
            digest["top"] = []
            digest["outliers"] = {"count": 0, "high": [], "low": []}
        return digest


ALL_LENSES = (SectorLens, IndustryLens, CountryLens, MacroLens, FactorLens)
