"""The four analyst lenses: sector, industry, country, macro.

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

from .digest import as_pairs, build_digest, digest_envelope, render_scaled
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
    """Group entities by Entity.country; unknown country groups as "??"."""

    name = "country"

    def scope(self) -> dict:
        return {"group_by": "country", "concepts": list(AGG_CONCEPTS)}

    def _classify(self, entity_id, entity, latest, entity_citations):
        if entity is None or not entity.country:
            return "??", entity_citations.get(entity_id)
        return entity.country, entity_citations.get(entity_id)


class MacroLens:
    """Per-region macro time series tails from "XX MACRO" pseudo-entities.

    A macro digest group carries, per concept, the last ``tail`` points of
    the series — each point a ``{"period", "value", "citation"}`` triple —
    so an analyst LLM sees level *and* trajectory, with every point
    independently verifiable. Points dropped by the tail cap are counted
    in ``truncated``.
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

        dropped = 0
        keys = sorted(regions)
        if len(keys) > max_groups:
            dropped += len(keys) - max_groups
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
                    dropped += len(ends) - tail
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
                dropped += len(cited) - max_citations_per_group
                cited = cited[:max_citations_per_group]
            groups.append({"key": region, "entities": len(regions[region]),
                           "series": concept_series, "citations": cited})

        return digest_envelope(self.name, self.scope(), groups,
                               unclassified={"entities": unclassified},
                               dropped=dropped, out_of_universe=out_of_universe)


ALL_LENSES = (SectorLens, IndustryLens, CountryLens, MacroLens)
