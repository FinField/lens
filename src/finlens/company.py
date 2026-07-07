"""CompanyLens — one company, everything an analyst needs, with provenance.

Where the classification lenses answer "how does the group look?", this
lens answers "show me this company": the latest value per ``(concept,
unit)`` sorted into small statement buckets, every derived/consensus fact
with its full ``derived_from`` chain (the provenance chain IS the point —
a verifier walks from a headline ratio back to the audited filing), and a
token-lean trailing history for duration concepts. Same read-model rules
as every other lens: no state, no mutation, exact rendered decimals,
deterministic ordering, no silent truncation.
"""
from __future__ import annotations

from typing import Iterable, Optional

from finfacts.model import Entity, FinFact

from .digest import as_pairs, digest_envelope, render_scaled
from .lenses import ticker_country

# Source kinds whose facts are provenance-chained rather than filed.
DERIVED_SOURCE_KINDS = ("finfield-derived", "finfield-consensus")

# The statement buckets, in statement-reading order. canonical_json sorts
# keys anyway; the tuple is the documented vocabulary.
BUCKETS = ("classification", "shares", "income", "balance", "cashflow",
           "per-share", "prices", "other")

# Small deterministic concept -> bucket map covering the concepts the real
# feed carries today plus the derive-layer inputs. Unknown concepts route
# visibly to "other" — the map can grow without breaking older digests.
# No open scraper mints base price concepts yet; "prices" is reserved for
# them. finfield:* concepts are classified by provenance (see
# is_derived_fact): scraped ones like finfield:sic are statement facts and
# bucket here, chained ones route to "derived".
CONCEPT_BUCKETS = {
    # classification
    "finfield:sic": "classification",
    # shares / float
    "dei:EntityPublicFloat": "shares",
    "dei:EntityCommonStockSharesOutstanding": "shares",
    "us-gaap:CommonStockSharesIssued": "shares",
    "us-gaap:CommonStockSharesOutstanding": "shares",
    "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic": "shares",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
    # income statement
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": "income",
    "us-gaap:Revenues": "income",
    "us-gaap:SalesRevenueNet": "income",
    "us-gaap:CostOfRevenue": "income",
    "us-gaap:GrossProfit": "income",
    "us-gaap:OperatingExpenses": "income",
    "us-gaap:CostsAndExpenses": "income",
    "us-gaap:OperatingIncomeLoss": "income",
    "us-gaap:NetIncomeLoss": "income",
    # balance sheet
    "us-gaap:Assets": "balance",
    "us-gaap:Liabilities": "balance",
    "us-gaap:StockholdersEquity": "balance",
    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "balance",
    "us-gaap:CashAndCashEquivalentsAtCarryingValue": "balance",
    "us-gaap:LongTermDebt": "balance",
    # cash flow
    "us-gaap:NetCashProvidedByUsedInOperatingActivities": "cashflow",
    "us-gaap:NetCashProvidedByUsedInInvestingActivities": "cashflow",
    "us-gaap:NetCashProvidedByUsedInFinancingActivities": "cashflow",
    "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": "cashflow",
    # per-share
    "us-gaap:EarningsPerShareBasic": "per-share",
    "us-gaap:EarningsPerShareDiluted": "per-share",
    "us-gaap:CommonStockDividendsPerShareDeclared": "per-share",
}


def is_derived_fact(fact: FinFact) -> bool:
    """True for provenance-chained facts — classified by *provenance*, not
    by concept namespace alone: derived/consensus source kinds, plus
    ``finfield:*`` concepts that actually carry a ``derived_from`` chain.
    A scraped ``finfield:sic`` (empty chain) is a statement fact and
    buckets like one."""
    return (fact.source.kind in DERIVED_SOURCE_KINDS
            or (fact.concept.startswith("finfield:")
                and bool(fact.derived_from)))


class CompanyLens:
    """One-company digest: bucketed latest facts, derived chains, history.

    Constructed for one ``entity_id`` (``"ticker:AIR US"``) — the instance
    carries its own scope, so it flows through ``snapshot_digest``
    unchanged: ``snapshot_digest(snap, CompanyLens("ticker:AIR US"))``.

    Digest payload, on the standard envelope (``groups`` stays ``[]`` —
    this lens does not aggregate a population):

    - ``"facts"``: latest base fact per ``(concept, unit)`` — same
      ``(period.end, cid)`` tie-break as every lens — bucketed by
      ``CONCEPT_BUCKETS``, each entry ``{"concept", "unit", "value",
      "period_end", "source_ref", "citation"}`` plus ``"fp"``/``"fy"``
      when the fact's period carries fiscal context; per-bucket cap with
      visible truncation (``truncated["facts"]``);
    - ``"derived"``: latest derived/consensus fact per ``(concept, unit)``
      as ``{"concept", "unit", "value", "period_end", "citation",
      "derived_from": [...]}`` (plus ``"fp"``/``"fy"`` when carried) — in
      the snapshot path ``derived_from`` holds knit-CIDs resolvable in the
      same ``@graph``. Capped at ``max_derived`` entries in sorted
      ``(concept, unit)`` order; drops count into ``truncated["derived"]``;
    - ``"history"``: for duration concepts with more than one period, the
      trailing ``history_tail`` points as ``{"period_end", "value"}``,
      nested ``{concept: {unit: [...]}}`` like every metric block. At most
      ``max_history_concepts`` ``(concept, unit)`` series, in sorted order;
      points dropped by either cap count into ``truncated["history"]``.

    Visibility counters keep their house meanings: ``unclassified`` counts
    the *other* entities whose facts the lens saw but did not interpret
    (a company lens over a whole snapshot scopes, it never silently
    drops), ``out_of_universe`` counts fact entities missing an entity
    record exactly as in the grouped lenses.

    Scope carries the company metadata when an entity record is known
    (``name``/``asset``/``cik``, plus its record CID as ``citation`` when
    supplied via ``entity_citations``); ``country`` falls back to the
    composite-ticker suffix (``"AIR US" -> "US"``) since real-feed entity
    records carry no country field.
    """

    name = "company"

    def __init__(self, entity_id: str, max_derived: int = 24,
                 max_history_concepts: int = 12) -> None:
        self.entity_id = entity_id
        self.max_derived = max_derived
        self.max_history_concepts = max_history_concepts

    def scope(self, entity: Optional[Entity] = None,
              citation: Optional[str] = None) -> dict:
        scope: dict = {"entity": self.entity_id}
        if entity is not None:
            for key in ("name", "asset", "cik"):
                value = getattr(entity, key)
                if value:
                    scope[key] = value
        country = (entity.country if entity is not None and entity.country
                   else ticker_country(self.entity_id.split(":", 1)[-1]))
        if country:
            scope["country"] = country
        if citation is not None:
            scope["citation"] = citation
        return scope

    def build(self, facts: Iterable, entities: Optional[Iterable[Entity]] = None,
              history_tail: int = 4, max_per_bucket: int = 16,
              entity_citations: Optional[dict] = None) -> dict:
        pairs = as_pairs(facts)
        entity_map = {e.entity_id: e for e in entities} if entities is not None else {}
        entity_citations = dict(entity_citations) if entity_citations else {}
        fact_entities = {fact.entity_id for fact, _ in pairs}
        out_of_universe = (len(fact_entities - set(entity_map))
                           if entities is not None else 0)
        # facts for other entities are scoped out — visibly, never silently
        other_entities = len(fact_entities - {self.entity_id})
        mine = [(fact, citation) for fact, citation in pairs
                if fact.entity_id == self.entity_id]

        dropped_facts = dropped_derived = dropped_history = 0

        # latest per (concept, unit): max (period.end, cid, citation) wins
        latest: dict = {}
        for fact, citation in mine:
            key = (fact.concept, fact.unit)
            rank = (fact.period.end, fact.cid, citation)
            if key not in latest or rank > latest[key][0]:
                latest[key] = (rank, fact, citation)

        def fiscal(fact: FinFact) -> dict:
            """``fp``/``fy`` context, only when the period carries it."""
            context = {}
            if fact.period.fiscal_period is not None:
                context["fp"] = fact.period.fiscal_period
            if fact.period.fiscal_year is not None:
                context["fy"] = fact.period.fiscal_year
            return context

        buckets: dict = {}
        derived: list = []
        for concept, unit in sorted(latest):
            _, fact, citation = latest[(concept, unit)]
            if is_derived_fact(fact):
                derived.append({
                    "concept": concept, "unit": unit,
                    "value": render_scaled(fact.value, fact.scale),
                    "period_end": fact.period.end,
                    "citation": citation,
                    "derived_from": list(fact.derived_from),
                    **fiscal(fact),
                })
            else:
                bucket = CONCEPT_BUCKETS.get(concept, "other")
                buckets.setdefault(bucket, []).append({
                    "concept": concept, "unit": unit,
                    "value": render_scaled(fact.value, fact.scale),
                    "period_end": fact.period.end,
                    "source_ref": fact.source.ref,
                    "citation": citation,
                    **fiscal(fact),
                })
        for bucket in sorted(buckets):
            if len(buckets[bucket]) > max_per_bucket:
                dropped_facts += len(buckets[bucket]) - max_per_bucket
                buckets[bucket] = buckets[bucket][:max_per_bucket]
        if len(derived) > self.max_derived:  # already sorted (concept, unit)
            dropped_derived += len(derived) - self.max_derived
            derived = derived[:self.max_derived]

        # trailing history for duration concepts: one point per
        # (concept, unit, period.end), ties resolve to the max (cid,
        # citation) so replays converge
        series: dict = {}
        for fact, citation in mine:
            if fact.period.start is None:
                continue
            key = (fact.concept, fact.unit, fact.period.end)
            rank = (fact.cid, citation)
            if key not in series or rank > series[key][0]:
                series[key] = (rank, fact)
        history: dict = {}
        # only multi-period duration series qualify; the concept cap keeps
        # the first max_history_concepts (concept, unit) series in sorted
        # order, counting every dropped point — never a silent drop
        qualifying = []
        for concept, unit in sorted({(c, u) for c, u, _ in series}):
            ends = sorted(end for c, u, end in series
                          if c == concept and u == unit)
            if len(ends) < 2:  # single-period: already in facts/derived
                continue
            qualifying.append((concept, unit, ends))
        for concept, unit, ends in qualifying[self.max_history_concepts:]:
            dropped_history += len(ends)
        for concept, unit, ends in qualifying[:self.max_history_concepts]:
            if len(ends) > history_tail:
                dropped_history += len(ends) - history_tail
                ends = ends[-history_tail:]
            history.setdefault(concept, {})[unit] = [
                {"period_end": end,
                 "value": render_scaled(series[(concept, unit, end)][1].value,
                                        series[(concept, unit, end)][1].scale)}
                for end in ends]

        digest = digest_envelope(
            self.name,
            self.scope(entity_map.get(self.entity_id),
                       entity_citations.get(self.entity_id)),
            groups=[],
            unclassified={"entities": other_entities},
            truncated={"facts": dropped_facts, "derived": dropped_derived,
                       "history": dropped_history},
            out_of_universe=out_of_universe,
        )
        digest["facts"] = buckets
        digest["derived"] = derived
        digest["history"] = history
        return digest
