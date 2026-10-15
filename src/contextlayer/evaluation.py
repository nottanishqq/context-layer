"""Retrieval evaluation: is the retriever returning the right documents?

A golden set pairs questions with the documents that answer them, labelled by
hand. Metrics are computed per question at the document level, then averaged:

- recall@k:    of the relevant documents, what fraction appear in the top k?
- precision@k: of the top k, what fraction are relevant?
- MRR:         1 / rank of the first relevant document (0 if none), averaged.

The same metrics exist as SurrealQL functions (fn::recall_at_k, ...) in the
schema; `evaluate(..., db=db)` computes them in the database instead, which
is a useful cross-check that both implementations agree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Callable

from contextlayer.db import Database


@dataclass
class GoldenItem:
    question: str
    relevant: list[str]  # document ids, e.g. "document:idempotency-keys"
    tags: list[str]


def load_golden(path: str | Path) -> list[GoldenItem]:
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        GoldenItem(i["question"], [f"document:{d}" for d in i["relevant"]], i.get("tags", []))
        for i in items
    ]


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    return len(set(retrieved[:k]) & set(relevant)) / len(relevant) if relevant else 0.0


def precision_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    return len(set(retrieved[:k]) & set(relevant)) / k if k else 0.0


def reciprocal_rank(retrieved: list[str], relevant: list[str]) -> float:
    for rank, doc in enumerate(retrieved, start=1):
        if doc in relevant:
            return 1.0 / rank
    return 0.0


@dataclass
class Scores:
    recall: float
    precision: float
    mrr: float


@dataclass
class Report:
    name: str
    k: int
    overall: Scores
    per_tag: dict[str, Scores]
    per_question: list[tuple[GoldenItem, list[str], Scores]]


def _db_scores(db: Database, retrieved: list[str], relevant: list[str], k: int) -> Scores:
    r, p, m = db.query(
        "RETURN fn::recall_at_k($r, $g, $k); RETURN fn::precision_at_k($r, $g, $k); RETURN fn::reciprocal_rank($r, $g);",
        {"r": retrieved, "g": relevant, "k": k},
    )
    return Scores(r, p, m)


def evaluate(
    name: str,
    retrieve: Callable[[str], list[str]],
    golden: list[GoldenItem],
    k: int = 5,
    *,
    db: Database | None = None,
) -> Report:
    """`retrieve` maps a question to a ranked list of document ids."""
    rows = []
    for item in golden:
        retrieved = retrieve(item.question)
        if db is not None:
            scores = _db_scores(db, retrieved, item.relevant, k)
        else:
            scores = Scores(
                recall_at_k(retrieved, item.relevant, k),
                precision_at_k(retrieved, item.relevant, k),
                reciprocal_rank(retrieved, item.relevant),
            )
        rows.append((item, retrieved, scores))

    def average(subset: list[Scores]) -> Scores:
        return Scores(mean(s.recall for s in subset), mean(s.precision for s in subset), mean(s.mrr for s in subset))

    tags = sorted({t for item in golden for t in item.tags})
    per_tag = {t: average([s for i, _, s in rows if t in i.tags]) for t in tags}
    return Report(name, k, average([s for _, _, s in rows]), per_tag, rows)


def format_reports(reports: list[Report]) -> str:
    """A comparison table: one row per retriever, overall and per tag recall."""
    tags = sorted({t for r in reports for t in r.per_tag})
    k = reports[0].k
    w = max(len("retriever"), *(len(r.name) for r in reports))
    header = f"{'retriever':<{w}} {'recall@' + str(k):>9} {'prec@' + str(k):>7} {'MRR':>6}  " + "  ".join(
        f"{t[:10]:>10}" for t in tags
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        o = r.overall
        lines.append(
            f"{r.name:<{w}} {o.recall:>9.3f} {o.precision:>7.3f} {o.mrr:>6.3f}  "
            + "  ".join(f"{r.per_tag[t].recall:>10.3f}" for t in tags)
        )
    lines.append(f"(per-tag columns are recall@{k})")
    return "\n".join(lines)
