"""Ask the SurrealDB documentation a question.

    uv run python examples/surrealdb_docs/ask.py "How should we upgrade from 2.6.2 to the latest version?"

Expects the docs ingested into the `surrealdb_docs` database (see GUIDE.md) and
ANTHROPIC_API_KEY set. Three steps:

1. Plan: Claude rewrites the question as a few documentation-style search queries.
   Real questions rarely use the words of the page that answers them ("how do we
   upgrade?" vs "v3-compatible export"), and one search can't reach every page a
   multi-part answer needs.
2. Retrieve: the library's ContextBuilder runs once per query; results are merged
   round-robin so every query is represented within the token budget.
3. Answer: Claude answers from the merged context, citing sources.

Without an API key, `--queries ... --context-only` runs step 2 with your own queries.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from contextlayer import ContextBuilder, Database, Retriever, Settings, get_embedder
from contextlayer.context import Answer
from contextlayer.db import parse_rid
from contextlayer.llm import ClaudeLLM
from contextlayer.retrieval import Hit

PLANNER = """You turn a question about SurrealDB into search queries for its documentation.
Write 3 to 5 short queries that together find everything needed to answer it: the things the
question names, the guides and commands for the task, and what it implies without naming
(for example the version-specific guide for an upgrade, the exact commands, or the latest
release). Use the vocabulary of documentation, not the asker's phrasing."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}

ANSWER = """You are a SurrealDB support engineer answering a question from a user's team.
Use only the documentation in <sources>.

- Start with a direct recommendation, then give the steps in order, with the exact commands the docs give.
- For upgrades, say which versions each step applies to, what can and cannot be rolled back,
  and what changes when there are several instances or a cluster.
- Cite the sources you use inline with their index, like [2].
- If the sources don't cover part of the question, say so instead of guessing."""


class MultiQueryBuilder(ContextBuilder):
    """A ContextBuilder that retrieves for several queries and merges the hits."""

    queries: list[str] = []

    def retrieve(self, question: str) -> tuple[list[Hit], list[Hit]]:
        results = [super().retrieve(q) for q in self.queries or [question]]
        kept: list[Hit] = []
        seen: set[str] = set()
        # Round-robin: every query's best hit before any query's second, so the
        # budget isn't spent on one query's long tail.
        for rank in range(max(len(hits) for hits, _ in results)):
            for hits, _ in results:
                if rank < len(hits) and hits[rank].chunk_id not in seen:
                    seen.add(hits[rank].chunk_id)
                    kept.append(hits[rank])
        return kept, [hit for _, dropped in results for hit in dropped]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the SurrealDB documentation a question.")
    parser.add_argument("question")
    parser.add_argument("--db", default="surrealdb_docs")
    parser.add_argument("--queries", nargs="+", help="search queries to use instead of asking Claude for them")
    parser.add_argument("--context-only", action="store_true", help="print the context and stop; no answer")
    parser.add_argument("-k", type=int, default=5, help="hits per query before graph expansion")
    parser.add_argument("--budget", type=int, default=8000, help="token budget for the merged context")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    llm = None if args.queries and args.context_only else ClaudeLLM()
    queries = args.queries or llm.json(system=PLANNER, prompt=args.question, schema=PLAN_SCHEMA)["queries"][:5]
    queries = [args.question, *queries]  # the question itself is always one of the searches

    settings = dataclasses.replace(Settings(), surreal_db=args.db)
    embedder = get_embedder(settings.embedder, settings.embedding_model)
    with Database.connect(settings) as db:
        # One hop of graph expansion per query: with several queries, two hops mostly adds noise.
        builder = MultiQueryBuilder(Retriever(db, embedder), k=args.k, token_budget=args.budget, graph_depth=1)
        builder.queries = queries
        packet = builder.build(args.question)
        urls = {str(r["id"]): r["url"] for r in db.one(
            "SELECT id, metadata.url AS url FROM $ids",
            {"ids": [parse_rid(s.document_id) for s in packet.sources]},
        )}

    print("Searched for:\n" + "\n".join(f"  - {q}" for q in queries) + "\n")
    if args.context_only:
        print(packet.render())
        print(f"\n-- {len(packet.sources)} sources, ~{packet.tokens} tokens")
        return

    answer = Answer(llm.text(system=ANSWER, prompt=f"{packet.render()}\n\n<question>{args.question}</question>"), packet)
    print(answer.text)
    print("\nSources:")
    for s in packet.sources:
        mark = "*" if s in answer.cited else " "
        print(f" {mark}[{s.index}] {s.title}  {urls.get(str(parse_rid(s.document_id)), '')}")


if __name__ == "__main__":
    main()
