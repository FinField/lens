# finlens

Analyst-LLM lenses over the FinField fact web — sector, industry, country and
macro digests an LLM can reason over directly, with every number exact and
every aggregate citing the facts it came from.

An *analyst lens* is a pure read model: it takes a bag of
[finfacts](https://github.com/FinField/facts) (or a
[pulse](https://github.com/knitweb/pulse) `web_snapshot`), groups the entities
the way a human analyst would — by SIC sector or industry, by country, by
macro region — and emits one compact JSON **digest**: exact medians and
quartiles per group (scaled-integer math rendered as plain decimal strings,
never floats), the count of entities it could not classify, and the CIDs of
the facts backing every aggregate. Feed the digest to any LLM and it can
answer "how does this company's margin compare to its sector?" with numbers
it can prove.

## The Lens contract

finlens holds to the pulse Lens contract's four invariants: a lens is an
**ephemeral, read-only interpret layer** over a snapshot — it holds no state,
has **no write path**, and **never mutates** anything during interpretation,
so interpreting the same facts twice is byte-identical and the Web it read is
byte-identical after it read. And **every answer preserves provenance**: a
digest cites the `state_root` of the snapshot it was computed over plus the
fact CIDs behind each aggregate, so a verifier re-derives everything offline
with no trust in the lens.

## The four lenses

| Lens | Groups by | Emits per group |
| --- | --- | --- |
| `SectorLens` | SIC division (via each entity's `finfield:sic` fact) | median/p25/p75 of `finfield:revenue_ttm`, `finfield:net_margin_ttm`, `finfield:revenue_yoy` + fact-CID citations |
| `IndustryLens` | SIC major group (2-digit industry) | same metrics + citations |
| `CountryLens` | `Entity.country` (missing country → `"??"` group) | same metrics + citations |
| `MacroLens` | region, from `"XX MACRO"` pseudo-entities (`asset="macro"`) | time-series tails of `finfield:policy_rate`, `finfield:cpi_yoy`, `finfield:fx_usd` — one citation per point |

Quantiles are exact and deterministic: *lower interpolation* on the sorted
scaled-int list (element `(num*(n-1))//den`), the same "lower value wins"
convention as finknit's weighted median. Aggregation partitions by
`(concept, unit)` — a USD and a JPY revenue never share a median — so group
metrics nest as `{concept: {unit: {median, p25, p75, n}}}`. Caps never
truncate silently — a digest that drops groups, citations, or series points
says so in `"truncated": {"dropped": n}` — entities a lens cannot classify
are counted in `"unclassified"`, and fact entities missing from the entity
universe still aggregate (into the visible catch-all where classification
needs entity metadata) and are counted in `"out_of_universe"`. Every group
also cites its *grouping* provenance: the `finfield:sic` fact — or the
entity record, for the country/macro lenses — that decided its key.

### Producers

Plainly: **today no scraper mints `finfield:sic` or macro facts.** Both are
tracked in the [scrapers](https://github.com/FinField/scrapers) backlog —
issue #4 (SIC codes from SEC submissions) and issue #5 (ECB SDMX macro:
`policy_rate`/`cpi_yoy`/`fx_usd`). Until those land, `SectorLens`,
`IndustryLens` and `MacroLens` run on any facts that follow the documented
conventions: `finfield:sic` is one fact per entity (integer 4-digit SIC
code, scale 0, unit `"pure"` — public domain, exactly what SEC EDGAR
publishes per registrant), and macro series are scale-6 `"pure"` facts
published by `"XX MACRO"` pseudo-entities with `asset="macro"`.

## SectorLens end to end

```python
from finfacts.model import Entity, FactSet, FinFact, Period, Source
from finlens import SectorLens, snapshot_digest, to_message_payload

# 1. facts — from finscrapers, or hand-minted:
src = Source(kind="sec-companyfacts", ref="0000320193-25-000123", fetched="2026-07-06")
fs = FactSet(entity=Entity(ticker="AAPL US", country="US"))
fs.add(FinFact(entity_id="ticker:AAPL US", concept="finfield:sic",
               value=3571, unit="pure", period=Period(end="2025-12-31"), source=src))
fs.add(FinFact(entity_id="ticker:AAPL US", concept="finfield:revenue_ttm",
               value=391035000000, unit="USD", period=Period(end="2025-12-31"), source=src))

# 2a. plain path — no pulse needed:
digest = SectorLens().build(fs.facts, [fs.entity])
# {"kind": "finfield-lens-digest", "lens": "sector",
#  "groups": [{"key": "Manufacturing", "entities": 1,
#              "metrics": {"finfield:revenue_ttm": {"USD": {"median": "391035000000", ...}}},
#              "citations": ["ff1:..."]}],   # incl. the classifying finfield:sic fact
#  "unclassified": {"entities": 0}, "out_of_universe": 0, "state_root": None}

# 2b. snapshot path — interpret the woven P2P web (pulse installed):
from knitweb.core import crypto
from knitweb.fabric.web import Web
from knitweb.fabric.snapshot import web_snapshot
from finknit import FinFieldKnitweb

web = Web()
FinFieldKnitweb(crypto.generate_keypair()[0]).weave_factset(fs, web)
snap = web_snapshot(web)                      # read-only, deterministic
digest = snapshot_digest(snap, SectorLens())  # citations = knit-CIDs in snap["jsonld"]["@graph"]
assert digest["state_root"] == snap["state_root"]   # answer bound to the exact Web

# 3. hand it to an analyst agent:
payload = to_message_payload("pls1mynode", "finfield/lens/sector", digest)
```

The pulse runtime is strictly optional: everything except the snapshot/atom
edges (`snapshot_digest` consuming a real snapshot, `to_atoms`) is pure
stdlib on top of finfacts + finknit.

Part of the [FinField](https://github.com/FinField) field: [facts](https://github.com/FinField/facts) ·
[scrapers](https://github.com/FinField/scrapers) · [knit](https://github.com/FinField/knit) ·
[agents](https://github.com/FinField/agents) · [signals](https://github.com/FinField/signals) ·
[crypto](https://github.com/FinField/crypto)
