"""finlens — analyst-LLM lenses over the FinField fact web.

Sector, industry, country, macro, cross-sectional factor and one-company
digests an analyst LLM can consume directly, computed under the pulse Lens
contract's four invariants:

1. **Ephemeral interpret layer** — a lens is handed a read-only projection
   of facts (at most a ``web_snapshot``), interprets it, and is done; it
   holds no durable state and nothing it produces re-enters the fabric.
2. **Adapters are read models** — every surface here is a projection;
   there is no write path to the Web, its records, signatures, or feeds.
3. **No mutation during interpretation** — digests are deterministic
   (sorted iteration, exact integer and string math, no wall-clock, no
   randomness): interpreting the same facts twice is byte-identical, and
   the Web a lens read is byte-identical to the Web after it read.
4. **Every answer preserves provenance** — a digest cites the fact CIDs
   backing its aggregates and the ``state_root`` of the snapshot it was
   computed over; a verifier re-derives everything offline.

Pure stdlib on top of finfacts + finknit; the pulse runtime is optional
and only touched by the snapshot/atom edges in :mod:`finlens.adapter`.
"""
from .digest import (  # noqa: F401
    DIGEST_KIND,
    as_pairs,
    build_digest,
    digest_envelope,
    metric_summary,
    quantile_lower,
    render_scaled,
)
from .sectors import (  # noqa: F401
    SIC_CONCEPT,
    SIC_DIVISIONS,
    SIC_MAJOR_GROUPS,
    industry_of,
    sector_of,
)
from .lenses import (  # noqa: F401
    AGG_CONCEPTS,
    ALL_LENSES,
    MACRO_CONCEPTS,
    CountryLens,
    FactorLens,
    IndustryLens,
    MacroLens,
    SectorLens,
    latest_by_entity_concept,
    ticker_country,
)
from .company import (  # noqa: F401
    BUCKETS,
    CONCEPT_BUCKETS,
    DERIVED_SOURCE_KINDS,
    CompanyLens,
    is_derived_fact,
)
from .adapter import (  # noqa: F401
    HAS_PULSE,
    snapshot_digest,
    to_atoms,
    to_message_payload,
)

__version__ = "0.1.0"
