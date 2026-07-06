"""The lens core: turn grouped financial facts into LLM-ready digests.

A *digest* is the answer unit a FinField lens hands to an analyst LLM. It
honors the pulse Lens contract: it is computed over a read-only projection
of facts (never a live Web), it is deterministic (sorted iteration, no
wall-clock, no randomness), every number is an int or a plain decimal
string (never a float), and it cites the CIDs of the facts that back its
aggregates plus the ``state_root`` of the snapshot it was computed over
(``None`` when the facts did not come from a snapshot).

Digest shape::

    {"kind": "finfield-lens-digest",
     "lens": <name>,
     "scope": {...},
     "groups": [{"key": k, "entities": n,
                 "metrics": {concept: {unit: {"median": s, "p25": s,
                                              "p75": s, "n": n}}},
                 "citations": [fact CIDs backing the medians]}, ...],
     "unclassified": {...},          # visible, never dropped silently
     "out_of_universe": n,           # fact entities missing a universe entry
     "truncated": {"dropped": n},    # only present when a cap dropped items
     "state_root": <64-hex or None>}

Metrics partition by ``(concept, unit)`` — a USD and a JPY revenue never
share a median.

Quantile rule (exact, deterministic): *lower interpolation* on the sorted
scaled-int list. For quantile ``num/den`` over ``n`` sorted values the
result is element ``(num * (n - 1)) // den`` — pure integer math, so every
node computes the byte-identical digest. For an even ``n`` the median is
the lower middle value, matching ``finknit.vote.weighted_median``.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from finfacts.model import FinFact

DIGEST_KIND = "finfield-lens-digest"

# A fact enters a lens either bare (citation = its own ff1 CID) or paired
# with the knit-CID it was decoded from (citation = the woven record's CID,
# resolvable in the snapshot's @graph).
FactRef = Union[FinFact, tuple]


def as_pairs(facts: Iterable[FactRef]) -> list:
    """Normalize facts to ``(FinFact, citation_cid)`` pairs."""
    pairs = []
    for item in facts:
        if isinstance(item, FinFact):
            pairs.append((item, item.cid))
        else:
            fact, citation = item
            pairs.append((fact, citation))
    return pairs


def quantile_lower(sorted_values: Sequence[int], num: int, den: int) -> int:
    """Exact quantile ``num/den`` by lower interpolation on sorted ints."""
    if not sorted_values:
        raise ValueError("quantile of empty list")
    if not 0 <= num <= den or den <= 0:
        raise ValueError(f"quantile {num}/{den} outside [0, 1]")
    return sorted_values[(num * (len(sorted_values) - 1)) // den]


def render_scaled(value: int, scale: int) -> str:
    """``value * 10^-scale`` as an exact plain decimal string.

    Pure string math — no Decimal context, so no silent rounding at any
    digit count. Trailing fractional zeros are stripped ("12.30" -> "12.3");
    zero renders as "0" at every scale.
    """
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    if scale <= 0:
        return sign + digits + "0" * -scale
    if len(digits) <= scale:
        digits = "0" * (scale - len(digits) + 1) + digits
    whole, frac = digits[:-scale], digits[-scale:].rstrip("0")
    return sign + whole + ("." + frac if frac else "")


def metric_summary(values: Sequence[int], scale: int) -> dict:
    """median/p25/p75/n over scaled ints at one common scale — all exact."""
    ordered = sorted(values)
    return {
        "median": render_scaled(quantile_lower(ordered, 1, 2), scale),
        "p25": render_scaled(quantile_lower(ordered, 1, 4), scale),
        "p75": render_scaled(quantile_lower(ordered, 3, 4), scale),
        "n": len(ordered),
    }


def digest_envelope(lens_name: str, scope: dict, groups: list,
                    unclassified: Optional[dict] = None, dropped: int = 0,
                    out_of_universe: int = 0,
                    state_root: Optional[str] = None) -> dict:
    """The common digest wrapper every lens shares.

    ``truncated`` appears only when a cap actually dropped items — a digest
    never truncates silently. ``unclassified`` is always present, and so is
    ``out_of_universe``: the count of distinct entity_ids that carried facts
    but had no entity record / universe entry (0 when none).
    """
    digest: dict[str, Any] = {
        "kind": DIGEST_KIND,
        "lens": lens_name,
        "scope": scope,
        "groups": groups,
        "unclassified": unclassified if unclassified is not None else {"entities": 0},
        "out_of_universe": out_of_universe,
        "state_root": state_root,
    }
    if dropped:
        digest["truncated"] = {"dropped": dropped}
    return digest


def build_digest(lens_name: str, scope: dict,
                 grouped_facts: Mapping[str, Iterable[FactRef]],
                 max_groups: int = 32, max_citations_per_group: int = 8,
                 group_entities: Optional[Mapping[str, int]] = None,
                 group_citations: Optional[Mapping[str, Iterable[str]]] = None,
                 unclassified: Optional[dict] = None,
                 out_of_universe: int = 0,
                 state_root: Optional[str] = None) -> dict:
    """Aggregate grouped facts into a quantile digest.

    ``grouped_facts`` maps a group key (sector, country, ...) to the facts
    backing that group, each a ``FinFact`` or ``(FinFact, citation_cid)``
    pair. Per group the facts partition by ``(concept, unit)`` — currencies
    never mix — are brought to their finest common scale, and summarize by
    the exact lower-interpolation quantile rule; the group cites the CIDs
    of every fact its metrics lean on, plus any extra ``group_citations``
    a lens supplies (e.g. the classification facts that decided the key).

    Groups iterate in sorted-key order; when caps drop groups or citations
    the digest says so in ``truncated: {"dropped": n}``.
    """
    dropped = 0
    keys = sorted(grouped_facts)
    if len(keys) > max_groups:
        dropped += len(keys) - max_groups
        keys = keys[:max_groups]

    groups = []
    for key in keys:
        pairs = as_pairs(grouped_facts[key])
        by_bucket: dict[tuple, list] = {}
        for fact, citation in pairs:
            by_bucket.setdefault((fact.concept, fact.unit), []).append((fact, citation))
        metrics: dict[str, dict] = {}
        citations = set(group_citations.get(key, ())) if group_citations else set()
        for concept, unit in sorted(by_bucket):
            rows = by_bucket[(concept, unit)]
            common = max(fact.scale for fact, _ in rows)
            values = [fact.value * 10 ** (common - fact.scale) for fact, _ in rows]
            metrics.setdefault(concept, {})[unit] = metric_summary(values, common)
            citations.update(citation for _, citation in rows)
        cited = sorted(citations)
        if len(cited) > max_citations_per_group:
            dropped += len(cited) - max_citations_per_group
            cited = cited[:max_citations_per_group]
        entities = (group_entities[key] if group_entities is not None
                    else len({fact.entity_id for fact, _ in pairs}))
        groups.append({"key": key, "entities": entities,
                       "metrics": metrics, "citations": cited})

    return digest_envelope(lens_name, scope, groups, unclassified=unclassified,
                           dropped=dropped, out_of_universe=out_of_universe,
                           state_root=state_root)
