# Course outline: Building a context layer for AI with SurrealDB and Python

One running project from start to finish: by the last lesson the learner has built the `contextlayer` package in this repository, piece by piece, against the Lumen Pay corpus. Each lesson adds one module, ends with a runnable check (a `ctx` command or a test), and opens with a failure from the previous lesson that this one fixes.

Every hands-on lesson has three parts, in this order:

1. **The SurrealQL first.** A `.surql` file you run in `surreal sql` or Surrealist, so the database idea stands on its own.
2. **The Python.** The module that wraps that SurrealQL, built up step by step.
3. **The measurement.** Something you run that shows whether the new piece helped.

| # | Lesson | Builds | Ends with | Key idea |
|---|---|---|---|---|
| 00 | Setup | `config.py`, `db.py` | `ctx init` | Run SurrealDB 3 locally. The SDK's `query()` returns only the first statement, so we wrap `query_raw()`. |
| 01 | What a context layer is | (reading) | — | Context windows, why models hallucinate, prompt engineering vs. context engineering, and what "one engine" buys you. |
| 02 | Schema for knowledge | `schema.surql` (document, chunk, links_to) | `INFO FOR DB` | SCHEMAFULL tables, record links vs. graph edges, `VALUE $before OR time::now()`. |
| 03 | Ingestion and chunking | `chunking.py`, `ingest.py` | `ctx ingest` | Front matter, wikilinks as edges, transactions, deterministic ids, re-ingesting safely. |
| 04 | Embeddings and vector search | `embeddings.py`, `Retriever.vector` | `ctx search --mode vector` | HNSW parameters, exact vs. approximate, `<|k,ef|>`, and the filtered-KNN pitfall measured against exact search. |
| 05 | Full-text search and BM25 | analyzer, `Retriever.fulltext` | `ctx search --mode fulltext` | Analyzers, why `@@` is AND, `@1,OR@`, `search::score` and `search::highlight`, where keywords beat vectors. |
| 06 | Hybrid search | `Retriever.hybrid` | built-in RRF == hand-rolled RRF | Reciprocal Rank Fusion by hand, then `search::rrf`, and why ranks rather than scores get fused. |
| 07 | Assembling context | `context.py` | `ctx context`, `ctx ask` | Relevance thresholds, token budgets, de-duplicating parents, numbered citations, RAG in one call. |
| 08 | Agent memory | `memory.py` | memory tests | Blended recall (similarity, recency, importance), reinforcement, TTL expiry, memory→document edges, and exact scans for small filtered sets. |
| 08b | Corrections and history | slots, `supersedes`, valid/record time | `ctx memory history`, `--as-of`, `--known-at` | Why a vector store returns both the old fact and the new one; supersede instead of overwrite; two clocks; retractions; provenance back to the conversation turn; slots vs. an LLM reconciler. |
| 09 | Text-to-SurrealQL | `text2surql.py` | `ctx sql` | Schema from DEFINE statements, dynamic few-shot, static checks, the VIEWER role as the real guardrail, retrying on errors. |
| 10 | Who sees what | `access.py`, permissions | the same search as Pat, then as Sam | Record access, `$auth`, table and field permissions, and why the agent shouldn't be the one aggregating across permissions. |
| 11 | The agent | `agent.py`, `llm.py` | `ctx chat` | A tool-use loop, memories attached to each turn, tool errors reported back to the model, prompt caching. |
| 12 | Measuring retrieval | `evaluation.py`, `golden.json` | `ctx eval` | Golden sets, recall@k, precision@k, MRR in Python and in SurrealQL, per-tag breakdowns. |
| 13 | Chunking strategies, measured | — | `ctx eval --strategies ...` | Fixed vs. sentence vs. section vs. parent/child, one strategy per database, retrieve small and generate large. |
| 14 | Graph RAG, multiple hops | `Retriever.expand`, `hybrid_graph` | multihop recall rises; other recall drops | Per-source pruning, `.{1..n+collect}`, `+shortest`, and deciding whether extra hops earn their tokens. |

## Running threads

- **One question carried through the course:** "Customers in Europe say payment confirmation emails show up hours late. How do we fix it?" Vector search finds the notification service. Hybrid search keeps it on top. Only graph expansion reaches the consumer-lag runbook, two links away.
- **One question nothing solves:** "Merchants see payouts that haven't shown up in their balance yet." It stays a miss in every configuration. Lesson 12 uses it to show why you measure instead of tuning to individual examples.
- **Each lesson's version of the golden-set numbers:** learners see the scores move as each piece lands.

## Open decisions

- **Format.** Markdown lessons with `.surql` and `.py` files to run (like SurrealDB University), Jupyter notebooks, or both.
- **Where learners start.** A `course/lessons/NN/` folder per lesson with starter files and a solution diff against this repository, or one repository that learners build up commit by commit.
- **The embedding model.** Real vectors from `fastembed` throughout (a ~130 MB download), or the SurrealDB course's approach of tiny hand-written vectors in lessons 04–06, with real ones afterwards.
