"""Retrieval: find the chunks that answer a question.

Three retrievers over the same `chunk` table:

- `vector`:   HNSW nearest-neighbour search over embeddings (meaning).
- `fulltext`: BM25 over an analyzed text index (exact words, codes, names).
- `hybrid`:   both, fused with Reciprocal Rank Fusion (RRF).

And one expander:

- `expand`:   follow `links_to` edges out from the documents retrieval found,
              keeping only the neighbours that look relevant to the question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from contextlayer.db import Database, parse_rid, rid_str
from contextlayer.embeddings import Embedder
from contextlayer.ingest import SEARCHABLE_STRATEGY


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    title: str
    kind: str
    text: str
    score: float  # higher is better; meaning depends on the retriever
    heading: str | None = None
    parent_text: str | None = None  # set when the chunk is a child of a larger section
    signals: dict[str, Any] = field(default_factory=dict)  # raw distance, bm25, rrf, hops...

    @property
    def generation_text(self) -> str:
        """What to hand the model: the parent section if there is one."""
        return self.parent_text or self.text


# Fields every retriever returns. `{p}` is "" when selecting from chunk rows and
# "id." when selecting from result rows that only hold record ids ($hits, $fused).
_PROJECTION = """
    {p}id AS id,
    {p}document AS document,
    {p}document.title AS title,
    {p}document.kind AS kind,
    {p}heading AS heading,
    {p}text AS text,
    {p}parent.text AS parent_text
"""


def _hits(rows: list[dict[str, Any]], score_key: str, signal_keys: tuple[str, ...]) -> list[Hit]:
    return [
        Hit(
            chunk_id=rid_str(r["id"]),
            document_id=rid_str(r["document"]),
            title=r["title"],
            kind=r["kind"],
            text=r["text"],
            heading=r.get("heading"),
            parent_text=r.get("parent_text"),
            score=float(r[score_key]),
            signals={k: r[k] for k in signal_keys if r.get(k) is not None},
        )
        for r in rows
    ]


class Retriever:
    def __init__(self, db: Database, embedder: Embedder, strategy: str = "section"):
        db.check_embedder(embedder)
        self.db = db
        self.embedder = embedder
        self.strategy = strategy

    @property
    def _stored_strategy(self) -> str:
        return SEARCHABLE_STRATEGY[self.strategy]

    def _filters(self, kinds: list[str] | None, prefix: str = "") -> str:
        clause = f"{prefix}strategy = $strategy"
        if kinds:
            clause += f" AND {prefix}document.kind IN $kinds"
        return clause

    def _knn(self, var: str, candidates: int, kinds: list[str] | None, ef: int = 64) -> str:
        """Two statements that leave the filtered nearest neighbours in `$var`.

        The HNSW search runs *without* a WHERE filter and over-fetches; the
        filter is applied to its results afterwards. Combining a filter with
        the KNN operator in one WHERE clause makes the index search the filtered
        subset of its graph, and on real (tightly clustered) text embeddings
        that can return far fewer than k rows. Unfiltered search, then filter,
        is predictable. Keep one chunking strategy per database and the filter
        rarely discards anything.

        The KNN operator takes literals, not parameters, so k/ef are formatted in.

        shortcut: table PERMISSIONS are also applied after the HNSW search picks its
        candidates, so a record user only gets the visible ones among them. The
        over-fetch hides this when a user can see most of the corpus; a user who can
        see a small slice of a large corpus gets fewer than k results, or none.
        """
        return f"""
            LET $nn = SELECT id, vector::distance::knn() AS distance FROM chunk
                WHERE embedding <|{int(candidates)},{int(ef)}|> $qv;
            LET ${var} = SELECT id, distance FROM $nn
                WHERE {self._filters(kinds, prefix="id.")}
                ORDER BY distance, id;
        """

    # ------------------------------------------------------------------ vector

    def vector(
        self,
        query: str,
        k: int = 5,
        *,
        kinds: list[str] | None = None,
        max_distance: float | None = None,
        candidates: int | None = None,
        exact: bool = False,
    ) -> list[Hit]:
        """Nearest chunks by cosine distance. `max_distance` drops weak matches.

        `exact=True` skips the HNSW index and compares the query with every
        chunk: slower, but the reference answer the index approximates.
        """
        params = {"qv": self.embedder.embed_query(query), "strategy": self._stored_strategy, "kinds": kinds}
        if exact:
            rows = self.db.one(
                f"""
                SELECT {_PROJECTION.format(p="")},
                    1 - vector::similarity::cosine(embedding, $qv) AS distance
                FROM chunk WHERE {self._filters(kinds)} AND embedding != NONE
                ORDER BY distance, id LIMIT {int(k)}
                """,
                params,
            )
        else:
            candidates = candidates or max(4 * k, 40)
            rows = self.db.one(
                self._knn("hits", candidates, kinds)
                + f"SELECT {_PROJECTION.format(p='id.')}, distance FROM $hits LIMIT {int(k)};",
                params,
            )
        if max_distance is not None:
            rows = [r for r in rows if r["distance"] <= max_distance]
        for r in rows:
            r["similarity"] = 1.0 - r["distance"]
        return _hits(rows, "similarity", ("distance",))

    # ---------------------------------------------------------------- fulltext

    def fulltext(
        self,
        query: str,
        k: int = 5,
        *,
        kinds: list[str] | None = None,
        operator: Literal["OR", "AND"] = "OR",
    ) -> list[Hit]:
        """BM25-ranked chunks. OR matches any query term; AND requires all of them."""
        match = "@1,OR@" if operator == "OR" else "@1@"
        sql = f"""
            SELECT {_PROJECTION.format(p="")},
                search::score(1) AS bm25,
                search::highlight('**', '**', 1) AS highlight
            FROM chunk
            WHERE {self._filters(kinds)} AND text {match} $qt
            ORDER BY bm25 DESC, id
            LIMIT {int(k)}
        """
        rows = self.db.one(sql, {"qt": query, "strategy": self._stored_strategy, "kinds": kinds})
        return _hits(rows, "bm25", ("bm25", "highlight"))

    # ------------------------------------------------------------------ hybrid

    def hybrid(
        self,
        query: str,
        k: int = 5,
        *,
        kinds: list[str] | None = None,
        candidates: int = 20,
        rrf_k: int = 60,
        fusion: Literal["builtin", "python"] = "builtin",
    ) -> list[Hit]:
        """Vector + BM25 candidates fused with Reciprocal Rank Fusion.

        RRF scores each chunk as the sum over lists of 1 / (rrf_k + rank). It
        uses only ranks, so it never has to reconcile a cosine distance with a
        BM25 score, which live on unrelated scales.
        """
        if fusion == "python":
            return self._hybrid_python(query, k, kinds=kinds, candidates=candidates, rrf_k=rrf_k)

        sql = self._knn("vs", max(4 * candidates, 40), kinds) + f"""
            LET $vs = array::slice($vs, 0, {int(candidates)});
            LET $ft = SELECT id, search::score(1) AS bm25 FROM chunk
                WHERE {self._filters(kinds)} AND text @1,OR@ $qt
                ORDER BY bm25 DESC, id LIMIT {int(candidates)};
            -- Fuse everything, then cut: many chunks tie on RRF score (first in one
            -- list scores the same as first in the other), and ties come back in no
            -- fixed order. Breaking them by id keeps results, and evaluations, repeatable.
            LET $fused = search::rrf([$vs, $ft], {2 * int(candidates)}, {int(rrf_k)});
            SELECT {_PROJECTION.format(p="id.")}, rrf_score, distance, bm25 FROM $fused
                ORDER BY rrf_score DESC, id LIMIT {int(k)};
        """
        rows = self.db.one(sql, {
            "qv": self.embedder.embed_query(query), "qt": query,
            "strategy": self._stored_strategy, "kinds": kinds,
        })
        return _hits(rows, "rrf_score", ("rrf_score", "distance", "bm25"))

    def _hybrid_python(self, query: str, k: int, *, kinds, candidates: int, rrf_k: int) -> list[Hit]:
        """The same fusion, written out by hand so you can see every step."""
        lists = [self.vector(query, candidates, kinds=kinds), self.fulltext(query, candidates, kinds=kinds)]
        fused: dict[str, Hit] = {}
        scores: dict[str, float] = {}
        for hits in lists:
            for rank, hit in enumerate(hits, start=1):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
                if hit.chunk_id in fused:
                    fused[hit.chunk_id].signals.update(hit.signals)
                else:
                    fused[hit.chunk_id] = hit
        ranked = sorted(fused.values(), key=lambda h: (-scores[h.chunk_id], h.chunk_id))[:k]  # ties by id
        for hit in ranked:
            hit.score = hit.signals["rrf_score"] = scores[hit.chunk_id]
        return ranked

    # ------------------------------------------------------------------- graph

    def expand(
        self,
        query: str,
        hits: list[Hit],
        *,
        depth: int = 2,
        per_hop: int = 1,
        seeds: int = 3,
        chunks_per_doc: int = 2,
        min_similarity: float = 0.0,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[Hit]:
        """Follow document links out from the top `seeds` documents in `hits`, hop by hop.

        At each hop, every document on the frontier keeps only its `per_hop`
        neighbours whose best chunk is most similar to the question. Pruning
        per source (not globally) matters: a neighbour that barely resembles
        the question may be the only bridge to the document that answers it.
        Without pruning at all, two hops from a well-linked page can reach
        most of the corpus.

        Returns the `chunks_per_doc` best chunks of each newly reached document,
        with the hop count and the document it was reached from.
        """
        qv = self.embedder.embed_query(query)
        frontier = list(dict.fromkeys(h.document_id for h in hits))[:seeds]
        # Only the seeds count as already found. A document further down `hits`
        # that a seed links to is worth surfacing: the link is extra evidence.
        seen = set(frontier)
        arrow = {"out": "->links_to->document", "in": "<-links_to<-document",
                 "both": "<->links_to<->document"}[direction]
        expanded: list[Hit] = []

        for hop in range(1, depth + 1):
            if not frontier:
                break
            edges = self.db.one(
                f"SELECT id AS from, {arrow} AS to FROM $frontier",
                {"frontier": [parse_rid(d) for d in frontier]},
            )
            came_from: dict[str, str] = {}
            for edge in edges:
                for target in edge["to"] or []:
                    if rid_str(target) not in seen:
                        came_from.setdefault(rid_str(target), rid_str(edge["from"]))
            candidates = list(came_from)
            if not candidates:
                break
            rows = self.db.one(
                f"""
                SELECT {_PROJECTION.format(p="")},
                    vector::similarity::cosine(embedding, $qv) AS similarity
                FROM chunk
                WHERE strategy = $strategy AND document IN $docs
                ORDER BY similarity DESC, id
                """,
                {"qv": qv, "strategy": self._stored_strategy,
                 "docs": [parse_rid(d) for d in candidates]},
            )
            by_doc: dict[str, list[dict[str, Any]]] = {}
            for r in rows:  # rows are sorted, so each document's list is best-first
                by_doc.setdefault(rid_str(r["document"]), []).append(r)
            kept, taken = [], {}  # taken: how many neighbours each source has kept
            for doc, doc_rows in by_doc.items():  # documents in order of their best chunk
                source = came_from[doc]
                if doc_rows[0]["similarity"] >= min_similarity and taken.get(source, 0) < per_hop:
                    taken[source] = taken.get(source, 0) + 1
                    kept.append((doc, source, doc_rows[:chunks_per_doc]))
            # Neighbours of better-ranked sources come first.
            rank = {doc: i for i, doc in enumerate(frontier)}
            kept.sort(key=lambda d: (rank[d[1]], -d[2][0]["similarity"]))
            for _, source, doc_rows in kept:
                for r in doc_rows:
                    r["hops"], r["via"] = hop, source
                expanded.extend(_hits(doc_rows, "similarity", ("similarity", "hops", "via")))
            frontier = [doc for doc, _, _ in kept]
            seen.update(frontier)
        return expanded

    def hybrid_graph(self, query: str, k: int = 5, *, seeds: int = 3, depth: int = 2, per_hop: int = 1,
                     kinds: list[str] | None = None) -> list[Hit]:
        """Hybrid search, then graph expansion from the top `seeds` documents.

        Order: the seed documents' chunks, then what the graph reached, then the
        rest of the hybrid results. Graph hits displace the tail of the hybrid
        ranking, which is exactly the trade an evaluation should judge.
        """
        hits = self.hybrid(query, max(2 * k, 10), kinds=kinds)
        seed_docs = list(dict.fromkeys(h.document_id for h in hits))[:seeds]
        expanded = self.expand(query, hits, depth=depth, per_hop=per_hop, seeds=seeds)
        head = [h for h in hits if h.document_id in seed_docs]
        tail = [h for h in hits if h.document_id not in seed_docs]
        return head + expanded + tail

    def neighbourhood(self, document_id: str, depth: int = 2) -> list[str]:
        """Every document reachable within `depth` outgoing links (no pruning)."""
        result = self.db.one(
            f"RETURN $doc.{{1..{int(depth)}+collect}}(->links_to->document)",
            {"doc": parse_rid(document_id)},
        )
        return [rid_str(r) for r in result or []]

    def shortest_path(self, from_id: str, to_id: str, max_depth: int = 6) -> list[str]:
        """The shortest chain of links between two documents ([] if unreachable)."""
        result = self.db.one(
            f"RETURN $a.{{..{int(max_depth)}+shortest=$b}}(->links_to->document)",
            {"a": parse_rid(from_id), "b": parse_rid(to_id)},
        )
        return [from_id] + [rid_str(r) for r in result or []] if result else []


def rank_documents(hits: list[Hit]) -> list[str]:
    """Collapse a chunk ranking into a document ranking (first appearance wins)."""
    return list(dict.fromkeys(h.document_id for h in hits))
