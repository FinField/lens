# finlens

Analyst-LLM lenses over the FinField fact web — sector, industry, country,
macro, cross-sectional factor and one-company digests an LLM can reason over
directly, with every number exact and every aggregate citing the facts it
came from.

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

## The six lenses

| Lens | Groups by | Emits per group |
| --- | --- | --- |
| `SectorLens` | SIC division (via each entity's `finfield:sic` fact) | median/p25/p75 of `finfield:revenue_ttm`, `finfield:net_margin_ttm`, `finfield:revenue_yoy` + fact-CID citations |
| `IndustryLens` | SIC major group (2-digit industry) | same metrics + citations |
| `CountryLens` | `Entity.country`, falling back to the composite-ticker suffix (`"AIR US"` → `"US"`); no two-part shape → `"??"` group | same metrics + citations |
| `MacroLens` | region, from `"XX MACRO"` pseudo-entities (`asset="macro"`) | time-series tails of `finfield:policy_rate`, `finfield:cpi_yoy`, `finfield:fx_usd` — one citation per point |
| `FactorLens` | one factor concept, cross-sectionally + per SIC sector | exact decile boundaries, per-sector metric blocks, deterministic fenced top/bottom-N entities + visible outliers — each with its citation and `period_end` |
| `CompanyLens` | one company (`CompanyLens("ticker:AIR US")`) | bucketed latest facts (with `fp`/`fy` fiscal context), derived facts with full `derived_from` chains, trailing history |

**Country fallback.** Entity records in the real FinField feed carry
`ticker`/`name`/`cik`/`asset` but **no country field**, so `CountryLens`
(and every country lookup, e.g. the `CompanyLens` scope) falls back to the
composite-ticker suffix: the second token of a two-token ticker is the
two-letter exchange country code (`"AIR US"` → `"US"`, `ticker_country()`).
The rule is strict and deterministic — one-token tickers (`"BTC"`),
pseudo-tickers (`"US MACRO"`) and any non-two-ASCII-uppercase-letter suffix
group visibly as `"??"`. An explicit `Entity.country` always wins.

Quantiles are exact and deterministic: *lower interpolation* on the sorted
scaled-int list (element `(num*(n-1))//den`), the same "lower value wins"
convention as finknit's weighted median. Aggregation partitions by
`(concept, unit)` — a USD and a JPY revenue never share a median — so group
metrics nest as `{concept: {unit: {median, p25, p75, n}}}`. Caps never
truncate silently — a digest that drops anything says so per kind in
`"truncated"`, e.g. `{"citations": 4174}` or `{"groups": 2, "history": 3}`,
listing only the kinds that dropped (the key is absent when nothing did) —
entities a lens cannot classify
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

## FactorLens — the cross-sectional factor view

`FactorLens(factor_concept="finfield:book_to_float_mcap", factor_unit="pure",
top_n=5, orientation="higher = cheaper (book value per unit of free-float
market cap)")` is the screen an analyst LLM reaches for on "is this cheap?"
questions. The cross-section is the latest factor fact per entity — facts
filter to `factor_unit` *before* latest-selection (a later foreign-unit fact
never shadows a valid factor fact; a factor never mixes units), then the
same `(period.end, cid)` tie-break as every lens. On top of the standard
envelope the digest carries:

- **`"deciles"`** — the 11 boundary values (min, d1..d9, max) of the
  cross-section: exact lower-interpolation quantiles at the finest common
  scale, rendered exact (`[]` when the cross-section is empty);
- **`"groups"`** — per SIC-division sector (via `finfield:sic` facts;
  entities with a factor value but no sic fact group visibly under
  `"unclassified"`) the standard `{concept: {unit: {median, p25, p75, n}}}`
  block for the factor, the entity count, and citations: the factor-fact
  CIDs plus the classifying sic CIDs, capped with visible truncation;
- **`"top"` / `"bottom"`** — the `top_n` entities by factor value as
  `{"entity", "value", "period_end", "citation"}`, ranked over the values
  inside the Tukey-style fences only. Fully deterministic ordering: `bottom`
  ascends by `(value, entity_id)`; `top` descends by value with ties broken
  by ascending entity_id;
- **`"outliers"`** — values outside the fences never rank but never
  disappear: `{"count": n, "high": [entries, the 3 most extreme],
  "low": [...]}` with entries shaped like top/bottom entries.

**Fences.** Real feeds carry degenerate ratios (a near-zero float turns
book-to-mcap into `910490.09`); unfenced, they own every top/bottom slot.
The lens computes Tukey-style fences on the cross-section's own deciles in
exact integer math at the common scale — `lo = d1 - 3*(d9 - d1)`,
`hi = d9 + 3*(d9 - d1)` — and reports them rendered in `scope["fences"]`.
Outliers still count in the deciles and their sector groups.

**Orientation.** A screen is unreadable without knowing which end is
"good", so `scope["orientation"]` states it — the default is the default
factor's reading, `"higher = cheaper (book value per unit of free-float
market cap)"`; pass your own when screening another concept.

**Staleness.** A ratio's inputs may straddle reporting instants —
`scope["staleness_note"]` says so (`"ratio inputs may differ by up to 400
days (derive guard)"`), and every top/bottom/outlier entry carries the
factor fact's `period_end`, so date mixing across the cross-section is
visible, never silent.

Entities without a usable factor value (no factor fact, or only foreign-unit
ones) are counted in the digest's `unclassified`. Works with any factor
concept — `finfield:earnings_to_float_mcap`, `finfield:capex_to_float_mcap`,
`finfield:net_margin_ttm`, … — and through `snapshot_digest` unchanged:
`snapshot_digest(snap, FactorLens("finfield:earnings_to_float_mcap"))`.

## CompanyLens — one company, everything, with provenance

`CompanyLens("ticker:AIR US")` answers "show me this company". The instance
carries its own scope, so it flows through `snapshot_digest` unchanged. Its
scope holds the company metadata when an entity record is known
(`name`/`asset`/`cik`, the record's CID as `citation`, and `country` — via
the composite-ticker fallback, since feed entity records carry none). The
payload, on the standard envelope (`groups` stays `[]`):

- **`"facts"`** — the latest *base* fact per `(concept, unit)`, sorted into
  small statement buckets (`classification`, `shares`, `income`, `balance`,
  `cashflow`, `per-share`, `prices`, `other` — the deterministic
  `CONCEPT_BUCKETS` map; unknown concepts route visibly to `"other"`), each
  entry `{"concept", "unit", "value", "period_end", "source_ref",
  "citation"}` plus `"fp"`/`"fy"` when the fact's period carries fiscal
  context; per-bucket cap (`max_per_bucket`) with visible truncation
  (`truncated["facts"]`);
- **`"derived"`** — every derived/consensus fact, latest per
  `(concept, unit)`, as `{"concept", "unit", "value", "period_end",
  "citation", "derived_from": [...]}`. Derived is a *provenance* call:
  source kind `finfield-derived`/`finfield-consensus`, or a `finfield:*`
  concept that actually carries a `derived_from` chain — a scraped
  `finfield:sic` (empty chain) is a statement fact and buckets under
  `classification`. The provenance chain IS the point: in the snapshot path
  `derived_from` holds knit-CIDs resolvable in the same `@graph`, so a
  verifier walks from `finfield:book_to_float_mcap` back to the audited
  float and equity lines. Capped at `max_derived=24` entries (ctor cap,
  sorted `(concept, unit)` order); drops count into `truncated["derived"]`;
- **`"history"`** — for duration concepts with more than one period, the
  trailing 4 (`history_tail`) points as `{"period_end", "value"}`, nested
  `{concept: {unit: [...]}}`; at most `max_history_concepts=12`
  `(concept, unit)` series (ctor cap, sorted order). Points dropped by
  either cap count into `truncated["history"]`.

Facts of *other* entities in the bag are scoped out visibly — counted in
`unclassified` — and `out_of_universe` keeps its usual meaning. `CompanyLens`
is not in `ALL_LENSES` (it needs an `entity_id` to exist).

## Analyst digests from the live feed

[`examples/feed_analyst_digests.py`](examples/feed_analyst_digests.py) runs
the lenses end-to-end over the real [feed](https://github.com/FinField/feed):
decode every record via `finknit.from_record`, run
`finfacts.derive.derive_all` per entity so the factor facts exist, then print
three LLM-ready digests — `FactorLens` over the whole cross-section,
`CompanyLens` for one entity, `CountryLens` (suffix-fallback countries) — to
stdout as canonical JSON, one per line. Deterministic: two runs are
byte-identical. Diagnostics (decode/reject counts, factor coverage, digest
sizes with a >32 KB warning) go to stderr.

```console
$ git clone https://github.com/FinField/feed /tmp/finfield-feed
$ python examples/feed_analyst_digests.py --feed /tmp/finfield-feed/feed
decoded 87313 facts + 6500 entity records (0 rejected, 0 foreign kinds) across 6500 fact entities
derived 4182 smart-pack facts; factor coverage 4182/6500 entities carry finfield:book_to_float_mcap
factor digest: 3890 bytes (3.7 KB)
company digest: 4837 bytes (4.7 KB)
country digest: 300 bytes (0.2 KB)
```

The FactorLens digest, truncated (`…`):

```json
{"kind": "finfield-lens-digest", "lens": "factor",
 "scope": {"factor": "finfield:book_to_float_mcap", "top_n": 5, "unit": "pure",
           "orientation": "higher = cheaper (book value per unit of free-float market cap)",
           "staleness_note": "ratio inputs may differ by up to 400 days (derive guard)",
           "fences": {"low": "-7.332494", "high": "9.386852"}},
 "deciles": ["-1809145.669291", "-0.16706", "0.04255", "0.192527", "0.348737",
             "0.536304", "0.754051", "1.018934", "1.399339", "2.221418",
             "910490.09901"],
 "groups": [{"key": "unclassified", "entities": 4182,
             "metrics": {"finfield:book_to_float_mcap":
                         {"pure": {"median": "0.536304", "n": 4182,
                                   "p25": "0.119458", "p75": "1.19793"}}},
             "citations": ["ff1:000cfe8b…", "…"]}],
 "top": [{"entity": "ticker:XELB US", "value": "9.222155",
          "period_end": "2026-03-31", "citation": "ff1:0d206402…"}, "…"],
 "bottom": [{"entity": "ticker:JWSMF US", "value": "-7.319836",
             "period_end": "2026-03-31", "citation": "ff1:fc5c3bc6…"}, "…"],
 "outliers": {"count": 151,
              "high": [{"entity": "ticker:NROM US", "value": "910490.09901",
                        "period_end": "2026-03-31", "citation": "ff1:0c8c3d09…"}, "…"],
              "low": [{"entity": "ticker:YBGJ US", "value": "-1809145.669291",
                       "period_end": "2026-03-31", "citation": "ff1:c31dee72…"}, "…"]},
 "unclassified": {"entities": 2318}, "out_of_universe": 0,
 "truncated": {"citations": 4174}, "state_root": null}
```

The degenerate ratios that used to own every top/bottom slot (`910490.09` —
a near-zero float) now sit outside the fences in the visible `outliers`
section, `count` exact, while `top`/`bottom` rank the investable range.

(All 4182 factor entities group under `"unclassified"` because no scraper
mints `finfield:sic` yet — see [Producers](#producers); the citation cap
drops visibly into `truncated`.) `--entity "ticker:MSFT US"` picks the
company digest's subject; `--llm` wraps each digest in the
`to_message_payload` message-bus envelope instead — `conforming` is `false`
by design there (no snapshot, no `state_root`; the conforming route is
`snapshot_digest` over a pulse `web_snapshot`), and the script prints a
one-line note saying exactly that.

Part of the [FinField](https://github.com/FinField) field: [facts](https://github.com/FinField/facts) ·
[scrapers](https://github.com/FinField/scrapers) · [knit](https://github.com/FinField/knit) ·
[agents](https://github.com/FinField/agents) · [signals](https://github.com/FinField/signals) ·
[crypto](https://github.com/FinField/crypto)
