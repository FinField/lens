"""Snapshot adapter: run a lens over a pulse ``web_snapshot`` — read-only.

This is the pulse edge of finlens. A Lens is never handed the live Web: it
receives the snapshot dict from ``knitweb.fabric.snapshot.web_snapshot``,
reads the deterministic JSON-LD ``@graph`` inside it, decodes the woven
``finfact-record`` nodes via :func:`finknit.from_record`, and answers with
a digest bound to the snapshot's ``state_root``. Nothing here weaves,
links, or writes — the adapter is a pure projection, and the citations it
emits are the knit-CIDs of the very ``@graph`` nodes it read, so a
verifier can resolve and re-hash every one offline.

The pulse runtime is strictly optional: :func:`snapshot_digest` and
:func:`to_message_payload` are plain dict-in/dict-out and run without
``knitweb`` installed (the snapshot itself is just a dict); only
:func:`to_atoms` imports the ``knitweb.lens`` atom layer, lazily.
"""
from __future__ import annotations

from importlib.util import find_spec
from typing import Any, Iterable, Optional

from finfacts.model import Entity, canonical_json
from finknit import KIND_ENTITY, KIND_FACT, InvariantError, from_record

from .digest import DIGEST_KIND

HAS_PULSE = find_spec("knitweb") is not None

_ENTITY_OPTIONAL = ("cik", "lei", "figi")


def _graph_nodes(snap: dict) -> Iterable[tuple]:
    """Yield ``(cid, record)`` for every node in the snapshot's @graph."""
    for node in snap["jsonld"]["@graph"]:
        node_cid = node.get("id", node.get("@id"))
        record = node.get("record")
        if node_cid is not None and isinstance(record, dict):
            yield node_cid, record


def _entity_from_record(record: dict) -> Entity:
    return Entity(
        ticker=record["ticker"],
        name=record.get("name", ""),
        country=record.get("country", ""),
        asset=record.get("asset", "equity"),
        **{key: record.get(key) for key in _ENTITY_OPTIONAL},
    )


def snapshot_digest(snap: dict, lens: Any,
                    entities: Optional[Iterable[Entity]] = None,
                    **caps: Any) -> dict:
    """Interpret one snapshot through one lens — the conforming Lens answer.

    Decodes every ``finfact-record`` node into a ``(FinFact, knit_cid)``
    pair (the knit-CID becomes the citation, resolvable in the @graph),
    reconstructs the entity universe from ``finfield-entity`` records when
    ``entities`` is not supplied, and runs ``lens.build``. Every decoded
    ``finfield-entity`` node also yields an ``entity_id -> knit_cid``
    citation map, passed to the lens so groups keyed on entity metadata
    (country, macro region) can cite the record that classified them.
    Foreign record kinds are skipped; records of our kinds that fail to
    decode are never silently swallowed — the digest reports them as
    ``"rejected": n``, whether or not ``entities`` was supplied. The
    digest's ``state_root`` is the snapshot's, binding the answer to the
    exact Web it was computed over.
    """
    pairs = []
    found_entities: list = []
    entity_citations: dict = {}
    rejected = 0
    for node_cid, record in _graph_nodes(snap):
        kind = record.get("kind")
        if kind == KIND_FACT:
            try:
                pairs.append((from_record(record), node_cid))
            except (InvariantError, KeyError, TypeError):
                rejected += 1
        elif kind == KIND_ENTITY:
            try:
                entity = _entity_from_record(record)
            except (KeyError, TypeError):
                rejected += 1
                continue
            found_entities.append(entity)
            # duplicate records for one entity: the max CID wins, so the
            # citation is order-independent
            if node_cid > entity_citations.get(entity.entity_id, ""):
                entity_citations[entity.entity_id] = node_cid

    universe = list(entities) if entities is not None else found_entities
    digest = lens.build(pairs, universe, entity_citations=entity_citations, **caps)
    digest["state_root"] = snap.get("state_root")
    digest["rejected"] = rejected
    return digest


def to_message_payload(sender: str, topic: str, digest: dict) -> dict:
    """A message-bus payload mirroring ``knitweb.lens.adapter``'s shape.

    Same envelope keys as ``KnitwebLensAdapter.to_message_payload`` —
    sender/topic/kind/content — with kind ``finfield-lens-digest`` and the
    digest rendered as canonical JSON, so any two nodes publish the
    byte-identical payload for the same interpretation. Per
    LENS_RLM_CONTRACT.md an answer without a ``state_root`` is
    non-conforming, so the payload says which it is: ``"conforming"`` is
    True exactly when the digest carries a state_root (i.e. it was
    computed over a snapshot, not a bare fact bag).
    """
    return {
        "sender": sender,
        "topic": topic,
        "kind": DIGEST_KIND,
        "lens": digest.get("lens"),
        "group_count": len(digest.get("groups", ())),
        "state_root": digest.get("state_root"),
        "conforming": digest.get("state_root") is not None,
        "content": canonical_json(digest),
    }


def to_atoms(digest: dict) -> list:
    """Project a digest into ``knitweb.lens`` atoms (pulse required).

    One head expression naming the lens and its state_root, then one
    expression per group carrying the group dict as a grounded record —
    the shape ``LensSpace`` queries expect.
    """
    if not HAS_PULSE:
        raise ImportError("knitweb (pulse) is required for lens atoms")
    from knitweb.lens.atom import ExpressionAtom, GroundedAtom, SymbolAtom

    atoms = [
        ExpressionAtom(
            SymbolAtom("FinFieldLens"),
            SymbolAtom(digest["lens"]),
            GroundedAtom(digest.get("state_root") or "", "Hash"),
        )
    ]
    for group in digest.get("groups", ()):
        atoms.append(
            ExpressionAtom(
                SymbolAtom("Group"),
                SymbolAtom(group["key"]),
                GroundedAtom(group, "Record"),
            )
        )
    return atoms
