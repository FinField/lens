"""Analyst digests from the live FinField feed — Factor, Company, Country.

End-to-end over real field data, pure stdlib on the FinField packages:

    feed shards (finfact-records + finfield-entity records)
      -> finknit.from_record            # decode + invariant-check (rejects counted, must be 0)
      -> finfacts.derive.derive_all     # per entity: finfield:book_to_float_mcap, *_ttm, ...
      -> finlens                        # FactorLens + CompanyLens + CountryLens digests

Deriving first is how an analyst gets `finfield:book_to_float_mcap` on raw
feed data — the feed carries audited base lines, the smart pack mints the
factor facts with full `derived_from` provenance, the lenses aggregate.

Prints three digests to stdout as canonical JSON, one per line —
deterministic: two runs over the same feed are byte-identical. Feed entity
records carry no country field, so the country digest groups by the
composite-ticker suffix fallback (`"AIR US" -> "US"`). Diagnostics go to
stderr, including per-digest size with a warning above 32 KB (digests must
stay LLM-context-sized).

Usage (from a checkout with the sibling repos, or pip-installed packages):

    git clone https://github.com/FinField/feed /tmp/finfield-feed
    python examples/feed_analyst_digests.py --feed /tmp/finfield-feed/feed
    python examples/feed_analyst_digests.py --feed ... --entity "ticker:MSFT US" --llm

With --llm each digest is wrapped in the message-bus payload an analyst LLM
agent consumes (`finlens.adapter.to_message_payload`); `conforming` is False
by design here — see the note the script prints.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

for sibling in ("facts", "knit"):  # sibling-checkout fallback; harmless when pip-installed
    p = Path(__file__).resolve().parents[2] / sibling / "src"
    if p.is_dir():
        sys.path.insert(0, str(p))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finfacts.derive import derive_all
from finfacts.model import Entity, FactSet, canonical_json
from finknit import KIND_ENTITY, KIND_FACT, InvariantError, from_record
from finlens import CompanyLens, CountryLens, FactorLens, to_message_payload

FACTOR = "finfield:book_to_float_mcap"
DIGEST_WARN_BYTES = 32 * 1024  # digests must stay LLM-context-sized


def load_feed(feed_dir: Path) -> tuple:
    """Decode every shard line: per-entity facts, entity records, counts."""
    per_entity: dict = {}
    entities: dict = {}
    decoded = rejected = foreign = 0
    for shard in sorted(feed_dir.glob("records-*.jsonl")):
        for line in shard.open(encoding="utf-8"):
            rec = json.loads(line, parse_float=Decimal)
            kind = rec.get("kind")
            if kind == KIND_FACT:
                try:
                    fact = from_record(rec)
                except (InvariantError, KeyError, TypeError):
                    rejected += 1
                    continue
                decoded += 1
                per_entity.setdefault(fact.entity_id, []).append(fact)
            elif kind == KIND_ENTITY:
                try:
                    # real-feed entity records: ticker/name/cik/asset, NO
                    # country — the lenses fall back to the composite-ticker
                    # suffix ("AIR US" -> "US") deterministically.
                    entity = Entity(ticker=rec["ticker"], name=rec.get("name", ""),
                                    asset=rec.get("asset", "equity"),
                                    cik=rec.get("cik"))
                except (KeyError, TypeError):
                    rejected += 1
                    continue
                entities.setdefault(entity.entity_id, entity)
            else:
                foreign += 1
    return per_entity, entities, decoded, rejected, foreign


def with_derived(per_entity: dict, entities: dict) -> tuple:
    """Base + smart-pack facts for every entity, in deterministic order."""
    facts: list = []
    derived_total = factor_entities = 0
    for entity_id in sorted(per_entity):
        base = per_entity[entity_id]
        entity = entities.get(
            entity_id, Entity(ticker=entity_id.removeprefix("ticker:")))
        derived = derive_all(FactSet(entity=entity, facts=list(base)))
        derived_total += len(derived)
        factor_entities += any(f.concept == FACTOR for f in derived)
        facts.extend(base)
        facts.extend(derived)
    return facts, derived_total, factor_entities


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feed", required=True, type=Path,
                    help="feed/ dir of a FinField/feed clone (records-*.jsonl)")
    ap.add_argument("--entity", default="ticker:AAPL US",
                    help="entity_id for the CompanyLens digest")
    ap.add_argument("--llm", action="store_true",
                    help="print to_message_payload envelopes instead of bare digests")
    args = ap.parse_args()

    per_entity, entities, decoded, rejected, foreign = load_feed(args.feed)
    print(f"decoded {decoded} facts + {len(entities)} entity records "
          f"({rejected} rejected, {foreign} foreign kinds) "
          f"across {len(per_entity)} fact entities", file=sys.stderr)
    if rejected:  # a healthy feed decodes clean — never aggregate a broken one silently
        print(f"error: {rejected} records rejected by from_record", file=sys.stderr)
        return 1

    facts, derived_total, factor_entities = with_derived(per_entity, entities)
    print(f"derived {derived_total} smart-pack facts; factor coverage "
          f"{factor_entities}/{len(per_entity)} entities carry {FACTOR}",
          file=sys.stderr)

    universe = [entities[key] for key in sorted(entities)]
    digests = [
        FactorLens().build(facts, universe),
        CompanyLens(args.entity).build(facts, universe),
        CountryLens().build(facts, universe),  # suffix-fallback countries
    ]

    if args.llm:
        print("note: conforming=false is expected — these digests were computed "
              "over the bare feed, not a snapshot (no state_root); the conforming "
              "route is finlens.adapter.snapshot_digest over a pulse web_snapshot.",
              file=sys.stderr)
    for digest in digests:
        blob = canonical_json(digest)
        size = len(blob.encode("utf-8"))
        tenths = size * 10 // 1024  # integer math only — no floats anywhere
        print(f"{digest['lens']} digest: {size} bytes ({tenths // 10}.{tenths % 10} KB)",
              file=sys.stderr)
        if size > DIGEST_WARN_BYTES:
            print(f"warning: {digest['lens']} digest exceeds 32 KB — "
                  f"tighten the lens caps to keep it LLM-context-sized",
                  file=sys.stderr)
        if args.llm:
            blob = canonical_json(to_message_payload(
                sender="finfield-example",
                topic=f"finfield/{digest['lens']}", digest=digest))
        print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
