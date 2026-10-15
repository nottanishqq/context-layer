# Context layer

A context layer for AI agents, built on SurrealDB 3 and Python. One database holds everything an agent needs to answer well: the documents, their chunks and embeddings, a full-text index, the links between documents, the agent's memory of each user, and the permissions that decide who may see what.

```
                 question
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
  vector search (HNSW)     full-text (BM25)          retrieval.py
        └───────────┬────────────┘
                    ▼
        Reciprocal Rank Fusion  ──►  graph expansion (links_to)
                    │
                    ▼
     relevance threshold + token budget  ◄──  agent memory (memory.py)
                    │
                    ▼
       <sources> with citation indexes          context.py
                    │
                    ▼
                 Claude  ──► tools: search, query_database (text-to-SurrealQL), remember
```

Every query runs as somebody. Run it as a record user and table permissions filter what comes back, including vector and full-text results.

## Quick start

You need Python 3.13+, [uv](https://docs.astral.sh/uv/), and the `surreal` binary (3.x) or Docker.

```bash
surreal start --user root --pass root memory
```

In a second terminal:

```bash
uv sync
uv run ctx init
uv run ctx ingest
uv run ctx users
uv run ctx search "Customers in Europe say confirmation emails arrive hours late"
uv run ctx context "Why were customers charged twice in March?"
uv run ctx eval
```

`ctx search` shows what each retriever returns. `ctx context` shows the exact text the model would receive. `ctx eval` compares retrievers and chunking strategies on a hand-labelled golden set. None of these need an API key.

`ctx ingest` treats the folder as the source of truth: re-running it updates changed documents and removes documents whose files are gone (pass `--keep` to skip the removal). `ctx init` records which embedding model filled the database, and every later command refuses a different one. Vectors from two models can't be compared, even when they have the same size.

To generate answers, copy `.env.example` to `.env`, set `ANTHROPIC_API_KEY`, then:

```bash
uv run ctx ask "What should on-call do if internal logins fail at 2am on the 1st?"
uv run ctx sql "Which documents link to the ledger service?"
uv run ctx chat --user pat@lumenpay.example --password demo-pat-2026
```

Agent memory keeps its history. A correction supersedes the old fact, and the old fact is kept:

```bash
uv run ctx memory remember "Pat is on the payments team." --subject user --attribute team --valid-from 2026-01-05
uv run ctx memory remember "Pat moved to the platform team." --subject user --attribute team --valid-from 2026-09-01
uv run ctx memory lookup user team
uv run ctx memory lookup user team --as-of 2026-06-01
uv run ctx memory history user team
```

The first lookup returns the platform team; the second, as of June, returns payments. `--known-at` asks what the agent believed at a given moment rather than what was true then. `ctx memory list --all` labels each memory current, ended or upcoming by its dates, and `ctx memory erase` deletes a user's memories together with the conversation turns they came from.

Run the tests (they start their own throwaway SurrealDB server and use an offline embedder):

```bash
uv run pytest
```

## What's where

| Module | What it does |
|---|---|
| [schema.surql](src/contextlayer/schema.surql) | Tables, HNSW and BM25 indexes, the document graph, memory, record access, permissions, and metric functions |
| [db.py](src/contextlayer/db.py) | Connection wrapper that returns every statement's result and surfaces the real error inside transactions |
| [embeddings.py](src/contextlayer/embeddings.py) | `Embedder` protocol; local `fastembed` model, or an offline hash embedder for tests |
| [chunking.py](src/contextlayer/chunking.py) | Fixed, sentence, section and parent/child chunking |
| [ingest.py](src/contextlayer/ingest.py) | Markdown + front matter → documents, chunks, embeddings and `links_to` edges, in one transaction per document |
| [retrieval.py](src/contextlayer/retrieval.py) | Vector, full-text, hybrid (built-in and hand-rolled RRF), graph expansion, shortest path |
| [memory.py](src/contextlayer/memory.py) | Agent memory: blended similarity/recency/importance recall, reinforcement, expiry, links to documents; corrections that supersede instead of overwrite, valid-time and record-time history, provenance |
| [context.py](src/contextlayer/context.py) | Relevance thresholds, token budget, de-duplication, numbered sources; `answer()` for RAG |
| [text2surql.py](src/contextlayer/text2surql.py) | Schema-from-`INFO FOR DB` prompts, few-shot by similarity, static guardrails, read-only execution, retry on error |
| [access.py](src/contextlayer/access.py) | Record users, team-scoped permissions, a read-only VIEWER login |
| [agent.py](src/contextlayer/agent.py) | A Claude tool-use loop over the context layer |
| [evaluation.py](src/contextlayer/evaluation.py) | Golden set, recall@k, precision@k, MRR, comparison tables |

The sample corpus in [data/corpus](data/corpus) is an internal wiki for a fictional payments company, Lumen Pay. It has 20 documents covering services, runbooks, policies and incident reports, linked with `[[wikilinks]]`. Three of the documents are team-only. [data/golden.json](data/golden.json) labels 24 questions as lexical, semantic, multihop or simple.

## Results on the sample corpus

`uv run ctx eval` with `BAAI/bge-small-en-v1.5`, recall@5 at the document level:

| retriever / chunking | recall@5 | MRR | lexical | semantic | multihop |
|---|---|---|---|---|---|
| vector / section | 0.847 | 0.771 | 0.917 | 0.786 | 0.667 |
| fulltext / section | 0.889 | 0.729 | 1.000 | 0.786 | 0.767 |
| hybrid / section | 0.910 | **0.865** | 0.917 | 1.000 | 0.667 |
| hybrid + graph / section | 0.882 | 0.854 | 0.917 | 0.857 | 0.733 |
| hybrid / fixed | **0.951** | 0.830 | 1.000 | 1.000 | 0.767 |
| hybrid + graph / parent_child | 0.924 | 0.835 | 1.000 | 0.857 | **0.833** |

What the table shows:

- **Each method wins somewhere.** Full-text wins on exact codes like `ERR_LEDGER_409`, vector search on paraphrases, and hybrid on ranking (MRR).
- **Graph expansion trades breadth for depth.** It raises multi-hop recall, but at k=5 its documents push out results that simpler questions needed.
- **Chunking interacts with everything else.** No strategy wins on every question type.

## SurrealDB 3.2 behaviours worth knowing

These came up while building this, and the code works around each one:

- **`@@` matches only documents containing every query term.** Natural-language questions usually match nothing. `text @1,OR@ $q` gives the BM25 behaviour you expect.
- **A filter in the same `WHERE` as the KNN operator can return far fewer than `k` rows on real text embeddings.** Unfiltered HNSW agreed with exact search on 100% of the golden questions; filtered HNSW sometimes returned 1 of 10. `Retriever` searches unfiltered, over-fetches, then filters. `MemoryStore` filters by an index first and then compares vectors exactly.
- **The KNN operator takes literals.** `<|$k,40|>` is a parse error.
- **`DELETE` then `INSERT` of the same record id inside one transaction fails with "already exists".** Ingestion upserts in place and deletes only stale chunks.
- **`NONE <= time::now()` is true.** `DELETE memory WHERE expires_at <= time::now()` would delete every memory that never expires.
- **`UPSERT ... CONTENT` replaces the whole record, so a field's `DEFAULT` doesn't survive it.** `created_at` uses `VALUE $before OR time::now()`.
- **Table permissions are applied after HNSW picks its candidates.** A record user only gets the visible ones among them, so someone who can see a small slice of a large corpus can get fewer than `k` results. The over-fetch covers it on the sample corpus; see the `shortcut:` note in `retrieval.py`.
- **Rows that tie under `ORDER BY` come back in no fixed order, and RRF produces many ties.** Ranked first by one retriever scores exactly the same as ranked first by the other. Without a tie-break, MRR on this golden set moved between 0.844 and 0.865 from one run to the next. Every ranking here ends with `, id`, and two runs of `ctx eval` print the same table.
- **The Python SDK's `query()` returns only the first statement's result,** and its embedded `mem://` engine is SurrealDB 2.x. This project uses `query_raw()` against a 3.x server.
