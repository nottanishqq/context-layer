"""Ingestion: turn source documents into documents, chunks, embeddings and links.

A Markdown file looks like this:

    ---
    title: Ledger service
    kind: service
    tags: [payments, ledger]
    visibility: team        # public | team | private
    team: payments
    ---
    The ledger service records every money movement. See [[idempotency-keys]].

`[[slug]]` (or `[[slug|label]]`) becomes a `links_to` edge between documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from contextlayer import chunking
from contextlayer.db import Database, rid
from contextlayer.embeddings import Embedder

_WIKILINK = re.compile(r"\[\[([a-z0-9\-]+)(?:\|([^\]]+))?\]\]")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class SourceDocument:
    slug: str
    title: str
    content: str
    kind: str = "note"
    tags: list[str] = field(default_factory=list)
    visibility: str = "public"
    team: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def links(self) -> list[tuple[str, str | None]]:
        """(target slug, anchor text) for every [[wikilink]], in order, deduplicated."""
        seen: dict[str, str | None] = {}
        for m in _WIKILINK.finditer(self.content):
            if m.group(1) != self.slug:
                seen.setdefault(m.group(1), m.group(2))
        return list(seen.items())

    @property
    def plain_text(self) -> str:
        """Content with [[slug|label]] rendered as `label` (or the slug)."""
        return _WIKILINK.sub(lambda m: m.group(2) or m.group(1).replace("-", " "), self.content)


def parse_markdown(path: Path) -> SourceDocument:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    if m := _FRONT_MATTER.match(raw):
        meta = yaml.safe_load(m.group(1)) or {}
        raw = raw[m.end():]
    known = {"title", "kind", "tags", "visibility", "team"}
    return SourceDocument(
        slug=meta.get("slug", path.stem),
        title=meta.get("title", path.stem.replace("-", " ").title()),
        content=raw.strip(),
        kind=meta.get("kind", "note"),
        tags=list(meta.get("tags", [])),
        visibility=meta.get("visibility", "public"),
        team=meta.get("team"),
        source=str(path.as_posix()),
        metadata={k: _plain(v) for k, v in meta.items() if k not in known | {"slug"}},
    )


def _plain(value: Any) -> Any:
    """YAML turns `2026-03-14` into a date, which the SDK can't encode; keep it as text."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def load_directory(directory: str | Path) -> list[SourceDocument]:
    docs = [parse_markdown(p) for p in sorted(Path(directory).glob("**/*.md"))]
    # A slug is the file name without its folder, so runbooks/ledger.md and
    # services/ledger.md would both become document:ledger and overwrite each other.
    first: dict[str, str | None] = {}
    for d in docs:
        if d.slug in first:
            raise ValueError(f"{first[d.slug]} and {d.source} both have the slug {d.slug!r}; "
                             "rename one or give it a `slug:` in its front matter")
        first[d.slug] = d.source
    return docs


# Strategy name -> the `chunk.strategy` value that is searched for it.
SEARCHABLE_STRATEGY = {"fixed": "fixed", "sentence": "sentence", "section": "section", "parent_child": "child"}
# Strategy name -> every `chunk.strategy` value it writes.
STORED_STRATEGIES = {"fixed": ["fixed"], "sentence": ["sentence"], "section": ["section"],
                     "parent_child": ["parent", "child"]}


class Ingestor:
    def __init__(self, db: Database, embedder: Embedder, strategies: Iterable[str] = ("section",)):
        db.check_embedder(embedder)
        self.db = db
        self.embedder = embedder
        self.strategies = list(strategies)

    def ingest(self, doc: SourceDocument) -> int:
        """Upsert one document and replace its chunks. Returns the number of chunks written."""
        doc_id = rid("document", doc.slug)
        rows: list[dict[str, Any]] = []
        for strategy in self.strategies:
            rows.extend(self._chunk_rows(doc, doc_id, strategy))

        stored = [s for strategy in self.strategies for s in STORED_STRATEGIES[strategy]]
        # One transaction: readers never see a document with half its chunks.
        # Chunk ids are deterministic, so re-ingesting upserts in place and only
        # deletes chunks that no longer exist (e.g. the document got shorter).
        self.db.query(
            """
            BEGIN TRANSACTION;
            UPSERT $doc CONTENT $data;
            DELETE chunk WHERE document = $doc AND strategy IN $strategies AND id NOT IN $rows.id;
            FOR $row IN $rows { UPSERT $row.id CONTENT $row; };
            COMMIT TRANSACTION;
            """,
            {
                "doc": doc_id,
                "data": {
                    "title": doc.title, "content": doc.content, "kind": doc.kind,
                    "tags": doc.tags, "visibility": doc.visibility, "team": doc.team,
                    "source": doc.source, "metadata": doc.metadata,
                },
                "strategies": stored,
                "rows": rows,
            },
        )
        return len(rows)

    def _chunk_rows(self, doc: SourceDocument, doc_id: Any, strategy: str) -> list[dict[str, Any]]:
        # The title goes into every chunk so a chunk is never anonymous.
        pieces = chunking.chunk(doc.plain_text, strategy)
        texts = [f"{doc.title}\n{c.text}" for c in pieces]
        searchable = [i for i, c in enumerate(pieces) if c.searchable]
        vectors = dict(zip(searchable, self.embedder.embed([texts[i] for i in searchable])))

        def chunk_id(position: int, kind: str) -> Any:
            return rid("chunk", f"{doc.slug}.{kind}.{position}")

        rows = []
        for i, c in enumerate(pieces):
            row: dict[str, Any] = {
                "id": chunk_id(c.position, c.strategy),
                "document": doc_id,
                "strategy": c.strategy,
                "position": c.position,
                "heading": c.heading,
                "text": texts[i],
                "token_count": c.token_count,
                "embedding": vectors.get(i),
            }
            if c.parent_index is not None:
                row["parent"] = chunk_id(pieces[c.parent_index].position, "parent")
            rows.append({k: v for k, v in row.items() if v is not None})
        return rows

    def link(self, doc: SourceDocument) -> int:
        """Replace this document's outgoing links. Targets that don't exist yet are skipped."""
        links = [{"in": rid("document", doc.slug), "out": rid("document", target), "anchor": anchor}
                 for target, anchor in doc.links]
        # One transaction: if the insert fails, the old links are still there.
        result = self.db.query(
            """
            BEGIN TRANSACTION;
            DELETE links_to WHERE in = $doc;
            LET $existing = (SELECT VALUE id FROM $targets WHERE id != NONE);
            INSERT RELATION INTO links_to ($links.filter(|$l| $l.out IN $existing));
            COMMIT TRANSACTION;
            """,
            {"doc": rid("document", doc.slug), "targets": [l["out"] for l in links], "links": links},
        )
        return len(result[-2] or [])  # the INSERT; the last result is COMMIT's

    def ingest_all(self, docs: list[SourceDocument], *, prune: bool = False) -> dict[str, int]:
        """Ingest every document, then link them (so forward references resolve).

        With `prune`, `docs` is the whole corpus: documents not in it are deleted,
        with their chunks. Their links and memory edges go with them.
        """
        chunks = sum(self.ingest(d) for d in docs)
        links = sum(self.link(d) for d in docs)
        removed = 0
        if prune:
            removed = len(self.db.query(
                """
                LET $keep = $ids;
                DELETE chunk WHERE document NOT IN $keep;
                DELETE document WHERE id NOT IN $keep RETURN BEFORE;
                """,
                {"ids": [rid("document", d.slug) for d in docs]},
            )[-1] or [])
        return {"documents": len(docs), "chunks": chunks, "links": links, "removed": removed}
