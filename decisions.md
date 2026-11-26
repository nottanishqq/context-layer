# Decisions

A log of the meaningful decisions behind this codebase: what was chosen, why, and what it cost. Entries are grouped by topic, not by date. When a decision was later reversed or revised, the entry says so and keeps the original reasoning, because the path matters as much as the destination in a course.

Each entry has the same parts:

- **Decision:** what we do.
- **Why:** the reason, with the evidence where there was some.
- **Trade-off:** what we gave up, or what we would do differently at another scale.

---

## 1. Platform and libraries

### 1.1 SurrealDB as the only store

- **Decision:** Documents, chunks, embeddings, the full-text index, the document graph, agent memory, users and permissions all live in one SurrealDB database.
- **Why:** The course is about a context layer, and its central claim is that retrieval, graph, memory and access control work better together than stitched across a vector DB, a search engine, a graph DB and an auth service. One engine means one query can filter vector results by permissions, and one transaction can write a document and its chunks.
- **Trade-off:** We depend on one database's maturity. Several 3.2 behaviours had to be worked around (see section 9). A team that already runs Postgres + pgvector would pay a migration cost to adopt this.

### 1.2 A SurrealDB 3.x server, not the SDK's embedded mode

- **Decision:** Everything, including the tests, talks to a real `surreal` server over WebSocket (`ws://.../rpc`).
- **Why:** The Python SDK's embedded `mem://` engine is SurrealDB 2.x. It rejects 3.x syntax this project relies on: `FULLTEXT` indexes, `search::rrf`, `@1,OR@`. Teaching 3.x syntax that the test engine can't run would be worse than needing a server.
- **Trade-off:** Learners must install the `surreal` binary or run Docker. The tests start their own throwaway server (`tests/conftest.py`), so this costs about a second per test session, not a manual step.

### 1.3 Python 3.13 with uv

- **Decision:** `uv` manages the environment and lockfile. Python is pinned to 3.13 (`.python-version`).
- **Why:** The user asked for Python. uv gives a reproducible install with one command (`uv sync`) and a lockfile, which matters when many learners follow the same lessons.
- **Trade-off:** Learners who only know pip/venv meet one new tool. Python 3.13 is newer than some corporate machines allow.

### 1.4 `fastembed` with `BAAI/bge-small-en-v1.5` for embeddings

- **Decision:** Embeddings are computed locally with fastembed (ONNX runtime), using bge-small (384 dimensions).
- **Why:** No API key, no cost per call, works offline after a one-time download of about 130 MB. bge-small is small enough to run on a laptop CPU in seconds for the sample corpus, and good enough to show real semantic search. fastembed avoids pulling in PyTorch.
- **Trade-off:** A small model has weaker recall than hosted models. Some golden-set questions fail partly because of it (for example the "payouts not reflected in balance" question). Everything depends on the `Embedder` protocol, so swapping models is one class.

### 1.5 An offline `HashEmbedder` for tests

- **Decision:** Tests use a deterministic bag-of-words hash embedder (`embeddings.HashEmbedder`), not the real model.
- **Why:** Tests should be fast, offline and deterministic. Shared words produce similar vectors, which is enough to test the mechanics (ingestion, ranking, fusion, permissions).
- **Trade-off:** Hash vectors know nothing about meaning, and their similarity scores are much lower than bge-small's. Thresholds tuned for bge-small (the 0.45 relevance distance, the 0.5 memory similarity floor) have to be overridden in some tests. Quality claims come from `ctx eval` with the real model, never from the unit tests.

### 1.6 Claude through the official `anthropic` SDK

- **Decision:** Generation, the agent, text-to-SurrealQL and the memory reconciler call Claude Opus 5.5 (`claude-opus-5-5`) through `anthropic.Anthropic().beta.messages.create`.
- **Why:** The default and most capable current model. The beta endpoint is used only so the request can carry `fallbacks="default"` (with the `server-side-fallback-2026-07-01` header): if a safety classifier declines a request, the API retries it on a suitable model instead of failing.
- **Trade-off:** Opus 5.5 costs more per token than Sonnet or Haiku. `CL_LLM_MODEL` switches models without code changes. The beta surface can change.

### 1.7 Effort `medium`, prompt caching on

- **Decision:** `ClaudeLLM` sends `output_config.effort = "medium"` and top-level `cache_control: ephemeral`.
- **Why:** RAG answers and short SurrealQL generation don't need deep reasoning. Medium is Opus 5.5's default, set explicitly so it doesn't drift. Caching makes the stable prefix (system prompt and tools) cheap across the turns of an agent loop.
- **Trade-off:** Hard multi-hop questions might do better at `high`. Not measured yet, because there is no API key in the build environment.

### 1.8 PyYAML and python-dotenv; no CLI framework

- **Decision:** PyYAML parses Markdown front matter. python-dotenv loads `.env`. The CLI uses the standard library's `argparse`.
- **Why:** Front matter is YAML, and hand-parsing it is a bug farm. `.env` files are what learners expect. `argparse` is enough for a dozen subcommands and adds no dependency.
- **Trade-off:** argparse help output is plainer than Typer or Click. Accepted, to keep the dependency list short.

### 1.9 Not using SurrealDB's Agent Memory product

- **Decision:** Agent memory is built by hand on ordinary tables (`memory.py`), not on SurrealDB's hosted Agent Memory product.
- **Why:** That product is closed source and waitlist-only, so a course anyone can follow can't depend on it. Building memory yourself is also what teaches how it works. The user confirmed this choice.
- **Trade-off:** We don't get its extras (typed entity extraction, response caching, prompt-injection scanning). Corrections and history were added ourselves (section 6).

---

## 2. Database access layer (`db.py`)

### 2.1 Wrap `query_raw()` instead of using the SDK's `query()`

- **Decision:** `Database.query()` calls the SDK's `query_raw()` and returns every statement's result. `Database.one()` returns the last statement's result.
- **Why:** The SDK's `query()` returns only the first statement's result. Almost every query here is multi-statement (`LET ...; LET ...; SELECT ...`).
- **Trade-off:** One more small class to read. It earns its place by hiding raw RPC envelopes from every other module.

### 2.2 Report the statement that actually failed

- **Decision:** When a query fails, `Database.query()` skips the "query was not executed due to a failed transaction" results and raises the first real error.
- **Why:** Inside a transaction, every statement reports failure. Without this, the error shown is always the useless "failed transaction" message, never the cause.
- **Trade-off:** None found.

### 2.3 One schema file, idempotent statements

- **Decision:** The whole schema is one `schema.surql` file. Tables, fields, functions and access use `DEFINE ... OVERWRITE`; indexes use `IF NOT EXISTS`.
- **Why:** Learners can read the data model in one place, and re-running `ctx init` is always safe. Indexes use `IF NOT EXISTS` because overwriting an index rebuilds it.
- **Trade-off:** An index definition change (say, a new HNSW dimension) is not applied by re-running init. That is exactly why the embedding-model check exists (2.5).

### 2.4 The embedding dimension is templated into the schema

- **Decision:** `{{EMBEDDING_DIM}}` in `schema.surql` is replaced with the embedder's dimension when the schema is applied.
- **Why:** `HNSW DIMENSION` takes a literal, and the dimension depends on the model.
- **Trade-off:** A text substitution instead of a parameter. Acceptable for one value.

### 2.5 Record the embedding model; refuse a different one

- **Decision:** `apply_schema()` stores the model name and dimension in `meta:embedding`. `Ingestor`, `Retriever` and `MemoryStore` all call `check_embedder()` and raise `EmbedderMismatch` if the model differs.
- **Why:** Vectors from two models can't be compared. A model with the same dimension mixes in with no error and turns search into noise. Learners are told to "swap in a real model", so this was going to happen. (Audit finding, fixed.)
- **Trade-off:** One extra query when each of those objects is created. Changing models now requires `ctx reset` and re-ingesting, which is the correct cost.

---

## 3. Ingestion (`ingest.py`, `chunking.py`)

### 3.1 Markdown with YAML front matter as the source format

- **Decision:** Documents are Markdown files. Front matter carries title, kind, tags, visibility and team. `[[slug]]` wikilinks become graph edges.
- **Why:** Markdown is what internal wikis export, it is readable in a course, and wikilinks give a real graph to traverse without an entity-extraction step.
- **Trade-off:** One source type. A production context layer would also ingest tickets, databases and SaaS tools (noted as future work).

### 3.2 Deterministic chunk IDs

- **Decision:** A chunk's ID is `chunk:<slug>.<strategy>.<position>`.
- **Why:** Re-ingesting the same document targets the same records, so updates are upserts and nothing duplicates.
- **Trade-off:** Positions shift if a section is inserted in the middle of a document, so later chunks are rewritten. Fine at this scale.

### 3.3 Upsert chunks, delete only stale ones, inside one transaction

- **Decision:** `Ingestor.ingest()` runs `UPSERT` on the document, deletes chunks whose IDs aren't in the new set, then upserts each chunk, all in one transaction.
- **Why:** Readers never see a document with half its chunks. The obvious version (delete all chunks, then insert) fails in SurrealDB 3.2: deleting and re-inserting the same record ID inside one transaction raises "already exists" (see 9.4).
- **Trade-off:** A `FOR` loop of upserts is slower than one bulk insert. Not noticeable at hundreds of chunks.

### 3.4 `created_at VALUE $before OR time::now()`

- **Decision:** The document's `created_at` field keeps its old value on update.
- **Why:** `UPSERT ... CONTENT` replaces the whole record, so a `DEFAULT` is not re-applied and `created_at` became `NONE`, which failed validation on the second ingest.
- **Trade-off:** None.

### 3.5 Link documents in a second pass, atomically

- **Decision:** `ingest_all()` ingests every document first, then calls `link()` for each. `link()` deletes a document's outgoing links and re-adds the ones whose targets exist, in one transaction.
- **Why:** A link can point at a document ingested later in the same run. The transaction means a failure can't leave a document with no links. (Transaction added after the audit; verified that delete-then-insert of edges works inside a 3.2 transaction, unlike ordinary records.)
- **Trade-off:** Links to documents that don't exist yet are skipped silently. Re-running ingest picks them up.

### 3.6 The folder is the source of truth (`prune`)

- **Decision:** `ingest_all(docs, prune=True)` deletes documents (and their chunks) that are no longer in the folder. `ctx ingest` uses it by default; `--keep` turns it off.
- **Why:** Without it, a deleted file stays searchable and citable forever. (Audit finding, fixed.)
- **Trade-off:** Pointing `ctx ingest` at a subfolder would remove everything else. That's why `--keep` exists.

### 3.7 Refuse duplicate slugs

- **Decision:** `load_directory()` raises if two files would get the same slug (the file name without its folder).
- **Why:** `runbooks/ledger.md` and `services/ledger.md` would both become `document:ledger` and silently overwrite each other. (Audit finding, fixed.)
- **Trade-off:** The user must rename a file or set `slug:` in its front matter.

### 3.8 Four chunking strategies, kept side by side

- **Decision:** `fixed` (word windows with overlap), `sentence` (whole sentences up to a budget), `section` (one chunk per heading section, the default) and `parent_child` (small children for search, linked to large parents for generation).
- **Why:** The chunking lesson compares them on the same corpus with the same golden set. Section was made the default because it follows the author's structure. The evaluation later backed that up: hybrid search over section chunks had the best MRR (0.865), though fixed and sentence chunks had higher recall@5.
- **Trade-off:** More code than one strategy. Each is a small pure function, so it's easy to read and test.

### 3.9 The document title is prepended to every chunk

- **Decision:** Chunk text is `"<title>\n<chunk text>"`.
- **Why:** A chunk on its own ("Owners: the payments team.") doesn't say what it is about. The title fixes that for both vector and keyword search.
- **Trade-off:** A few extra tokens per chunk.

### 3.10 Split long sections; keep tables and lists intact

- **Decision:** `section()` splits sections over 400 tokens by sentence, keeping the heading on each piece. The sentence and parent/child chunkers keep tables, lists and code line by line. The fixed chunker keeps original whitespace.
- **Why:** bge-small reads at most 512 tokens and silently ignores the rest, so the end of a long section was invisible to vector search. Tables were being flattened into one unreadable line. (Both audit findings, fixed.)
- **Trade-off:** Section chunks are no longer guaranteed to be whole sections. The 400 limit is a heuristic tied to this model.

### 3.11 A cheap token estimate

- **Decision:** `count_tokens()` estimates tokens as words ÷ 0.75.
- **Why:** It's used for chunk sizes and the context budget, where a rough number is enough. A real tokenizer would add a dependency and a model-specific vocabulary.
- **Trade-off:** Budgets are approximate. Code and non-English text are underestimated.

---

## 4. Retrieval (`retrieval.py`)

### 4.1 Three retrievers plus a graph expander

- **Decision:** `vector` (HNSW), `fulltext` (BM25), `hybrid` (both, fused with Reciprocal Rank Fusion), and `expand` (follow document links).
- **Why:** Each wins on a different question type, which the evaluation shows: full-text on exact codes like `ERR_LEDGER_409`, vector on paraphrases, hybrid on ranking, graph on multi-hop. Teaching all four and measuring them is the course's backbone.
- **Trade-off:** More surface area than a single retriever.

### 4.2 `@1,OR@` for full-text search

- **Decision:** Full-text queries use `text @1,OR@ $q` by default; `operator="AND"` is available.
- **Why:** Plain `@@` matches only text containing every query term. Natural-language questions then match nothing at all.
- **Trade-off:** OR matches more noise, which BM25 ranking mostly pushes down.

### 4.3 Two-stage vector search: search unfiltered, then filter

- **Decision:** `_knn()` runs HNSW without a `WHERE` filter, over-fetches candidates (`max(4k, 40)`), and applies the strategy/kind filter afterwards.
- **Why:** Measured on the real corpus: a filter in the same `WHERE` as the KNN operator sometimes returned 1 row out of 10, while unfiltered HNSW matched exact search on 100% of golden questions. A test (`test_ann_matches_exact_search`) guards it.
- **Trade-off:** Over-fetching costs more per query, and a very selective filter can still under-fill. Mitigated by keeping one chunking strategy per database (4.4).

### 4.4 One chunking strategy per database

- **Decision:** `ctx ingest` writes one strategy; `ctx eval` builds a separate database per strategy.
- **Why:** With four strategies in one table, each strategy is about 20% of the vectors, and the post-filter in 4.3 would discard most candidates.
- **Trade-off:** Comparing strategies needs several databases. They're cheap.

### 4.5 Permissions filter after the vector search: accepted, documented

- **Decision:** Record-user permissions are applied by SurrealDB after HNSW picks its candidates. Marked with a `shortcut:` comment and tested.
- **Why:** Measured: with 3 candidates, Pat got 2 results because a hidden document took a slot. The default over-fetch of 40 covers the sample corpus.
- **Trade-off:** A user who can see a small slice of a large corpus gets fewer than k results, or none. The fix (pre-filtering by an indexed visibility field, or more over-fetch for restricted users) waits until a corpus is large enough to need it.

### 4.6 The KNN `k` is formatted into the query

- **Decision:** `<|{k},{ef}|>` is built with an f-string, always through `int()`.
- **Why:** SurrealQL's KNN operator rejects parameters (`<|$k,40|>` is a parse error).
- **Trade-off:** String-built SQL. `int()` makes injection impossible here, and it's the only place we do it.

### 4.7 Built-in `search::rrf` plus a hand-written version

- **Decision:** `hybrid(fusion="builtin")` uses SurrealDB's `search::rrf`. `fusion="python"` does the same fusion by hand. A test checks they agree exactly.
- **Why:** The built-in is the production path. The hand-written one exists so learners can see every step of the algorithm.
- **Trade-off:** Two implementations to keep in sync; the test enforces it.

### 4.8 RRF over score-based fusion

- **Decision:** Fuse by rank (`1 / (60 + rank)`), not by combining scores.
- **Why:** Cosine distance and BM25 scores live on unrelated scales. Ranks need no calibration.
- **Trade-off:** It throws away how confident each retriever was. The relevance threshold (5.2) brings some of that back.

### 4.9 Break ties by ID in every ranking

- **Decision:** Every `ORDER BY` in retrieval ends with `, id`. Hybrid fuses all candidates, then sorts and cuts in the query.
- **Why:** RRF produces many exact ties (first in one list scores the same as first in the other), and SurrealDB returns tied rows in no fixed order. MRR on the golden set moved between 0.844 and 0.865 from run to run. Learners must get the numbers the lessons show.
- **Trade-off:** The ID order is arbitrary but stable. That's the point.

### 4.10 Graph expansion: per-source pruning from the top seeds

- **Decision:** `expand()` starts from the top 3 seed documents. At each hop, each frontier document keeps only its best `per_hop` neighbours (by best-chunk similarity to the question). Only the seeds count as "already found".
- **Why:** The first version pruned globally and dropped `queue-platform`, the only bridge from the notification service to the consumer-lag runbook. A second bug skipped neighbours that appeared anywhere in the raw hits, even below the cut. Both were found by the multi-hop golden questions.
- **Trade-off:** At k=5, graph results push out lower hybrid results. Multi-hop recall rises (0.667 → 0.733 on sections); simple and semantic recall drop a little. That trade is exactly what the graph-RAG lesson teaches learners to measure. It's on in `ctx context` and the agent, and a separate retriever (`hybrid_graph`) in the evaluation.

### 4.11 Two chunks per reached document

- **Decision:** Expansion returns the top 2 chunks of each document it reaches.
- **Why:** One chunk was often a weak one (the runbook's "Confirm" step instead of its fix).
- **Trade-off:** More tokens per expansion. The small model still picks imperfect chunks sometimes; that's left visible, not tuned away.

---

## 5. Context assembly (`context.py`)

### 5.1 A `ContextBuilder` that owns the whole pipeline

- **Decision:** One class goes from question to the exact text the model reads: retrieve → threshold → expand → recall memories → budget → render.
- **Why:** This is the "context layer" in one place, which is what the course is about. `ctx context` prints its output with no API key needed.
- **Trade-off:** Many constructor parameters. Each one is a lesson's tuning knob.

### 5.2 Keep a hit if either retriever is confident

- **Decision:** A hybrid hit is kept if its vector distance is ≤ 0.45 or its BM25 score is ≥ 3.0.
- **Why:** RRF scores are relative, so they can't be thresholded. The raw signals can. A keyword-only match on an exact error code is still worth keeping.
- **Trade-off:** Both numbers are tuned for bge-small and this corpus.

### 5.3 A token budget, with memories capped at a share

- **Decision:** Sources and memories share a 3,000-token budget. Memories may use at most 25%; extras go in `dropped_memories`. A parent section too big for the budget falls back to its child chunk.
- **Why:** Long memories could otherwise push out every source. (Audit finding, fixed.)
- **Trade-off:** A long, important memory can be dropped in favour of shorter ones.

### 5.4 Body text is not HTML-escaped

- **Decision:** Source and memory text is inserted as written. Only the title attribute is escaped.
- **Why:** Escaping turned `relayctl skip --group <group>` into `--group &lt;group&gt;`, and the model repeats commands exactly. The `<source>` tags are labels for the model, not a security boundary. (Reversal: the first version escaped everything. Audit finding, fixed.)
- **Trade-off:** A document containing `</source>` could confuse the delimiters. Accepted: the model copes, and broken commands for on-call engineers are worse.

### 5.5 Numbered sources and inline citations

- **Decision:** Sources are numbered `[1]`, `[2]`, ... and the system prompt asks for inline citations. `Answer.cited` parses `[2]`, `[1, 3]` and `[2-4]`.
- **Why:** Citations make answers checkable, and the CLI marks which sources were actually used. The first parser missed `[1, 3]` (audit finding, fixed).
- **Trade-off:** We trust the model to cite honestly; nothing verifies a citation supports its claim.

---

## 6. Agent memory (`memory.py`)

### 6.1 Recall blends similarity, recency and importance

- **Decision:** `score = 0.6·similarity + 0.2·recency + 0.2·importance`, where recency halves every 72 hours since last use.
- **Why:** Plain similarity returns stale or trivial memories as readily as important recent ones. This mirrors how the SurrealDB course frames agent memory.
- **Trade-off:** Three hand-picked weights. They are constructor parameters.

### 6.2 Exact scan for recall, not HNSW

- **Decision:** Recall filters to one agent and one user with an index (`memory_scope`), then computes cosine similarity exactly.
- **Why:** That filter is very selective, which is exactly where filtered HNSW misses results (4.3). One user's memories are few enough to compare directly. (Reversal: the first version used an HNSW index on memory; it was removed.)
- **Trade-off:** A user with hundreds of thousands of memories would make recall slow. Not a realistic size for one user.

### 6.3 A similarity floor, and reinforcement only on deliberate recall

- **Decision:** Recall ignores memories below `min_similarity` (default 0.5). Importance is raised only when the agent's `recall_memory` tool or `ctx memory recall` asks; building context doesn't reinforce.
- **Why:** Without a floor, recall always returned k memories, relevant or not, and every search raised their importance. After about 10 searches, the same few memories won every recall regardless of topic. (Audit finding, fixed.)
- **Trade-off:** 0.5 suits bge-small; other models need their own value. The hash embedder used in tests needs a lower one.

### 6.4 Near-duplicates are reinforced, not stored again

- **Decision:** A new memory within cosine distance 0.05 of a current one reinforces it instead.
- **Why:** Agents restate facts constantly. Duplicates waste the budget and distort recall.
- **Trade-off:** Two genuinely different facts that embed almost identically would merge. Rare at 0.05.

### 6.5 Corrections supersede; nothing is overwritten

- **Decision:** A correction creates a new memory, ends the old one's validity, and links them with a `supersedes` edge. Old rows stay.
- **Why:** A vector store that just appends returns both the old and the new fact, and the model guesses. Overwriting loses history ("where did Pat work in April?").
- **Trade-off:** The table only grows; `forget_user()` and TTLs are the ways to shrink it.

### 6.6 Two clocks: valid time and record time

- **Decision:** `valid_from`/`valid_until` record when a fact was true. `created_at`/`superseded_at` record when we learned it, and when we learned it had ended. `as_of` and `known_at` query each.
- **Why:** "What was true on 1 April?" and "what did the agent believe on 1 April?" are different questions.
- **Trade-off:** The time filter is subtle. The first version treated a superseded fact as no longer believed at all, so "where did Tobie work on 1 April?" returned nothing. A correction actually means "it was true until this date". Tests pin both questions down.

### 6.7 One definition of "current"

- **Decision:** Every read path uses the same time filter (`_AT_TIME` + `_IN_TIME`). Only reconciliation compares against the newest recorded value (`superseded_at IS NONE`), and its docstring says why.
- **Why:** Different code paths used to disagree for future-dated changes: `lookup` said Pat was on payments, `ctx memory list` said platform. (Audit finding, fixed.)
- **Trade-off:** None.

### 6.8 Slots for cheap corrections; an LLM judge for the rest

- **Decision:** A memory written with a `(subject, attribute)` slot replaces whatever fills that slot. Memories without a slot can be checked by an optional reconciler, an LLM call that answers supersede / duplicate / keep_both, but only for close neighbours.
- **Why:** Slots are deterministic and free, but the writer must know the slot. The judge handles free-text corrections ("actually he moved to SurrealDB") at the cost of an LLM call, so it's limited to neighbours within `conflict_distance`.
- **Trade-off:** Two mechanisms to explain. The agent's prompt tells the model when to use slots.

### 6.9 Provenance through conversation turns

- **Decision:** The agent logs each message as a `turn` record. A memory points at the turn it came from (`source_turn`) and carries a `confidence`.
- **Why:** "Why do you think that?" needs an answer, and erasure needs to know what to erase.
- **Trade-off:** Turns are stored in full, so they hold the user's raw words. `forget_user()` deletes them with the memories.

### 6.10 Erasure deletes memories and turns

- **Decision:** `forget_user()` (and `ctx memory erase`) deletes a user's memories and conversation turns. Record users may delete their own turns.
- **Why:** Deleting only the memories left the raw words ("I'm on the payments team now") in the turn log forever. (Audit finding, fixed.)
- **Trade-off:** Erasure is total; there is no "forget this one fact but keep its history".

### 6.11 Concurrent corrections: accepted, marked as a shortcut

- **Decision:** `remember()` reads the slot, then writes, in two steps. Marked with a `shortcut:` comment.
- **Why:** Two processes correcting the same slot at the same time could leave two current values. With one learner at a time this can't happen.
- **Trade-off:** For many concurrent users, the slot read must move inside the write transaction.

---

## 7. Text-to-SurrealQL and access control (`text2surql.py`, `access.py`)

### 7.1 The schema prompt comes from the database itself

- **Decision:** The prompt's schema section is built from `INFO FOR DB` and `INFO FOR TABLE`, minus permissions, embedding fields and internal tables.
- **Why:** A hand-written schema description drifts from the real schema. This one can't.
- **Trade-off:** `DEFINE` statements are less friendly than a curated description. The few-shot examples make up for it.

### 7.2 Few-shot examples chosen by similarity

- **Decision:** `data/surql_examples.json` holds question → query pairs. The 3 closest to the question go in the prompt.
- **Why:** Relevant examples teach the model this schema's idioms (backticked IDs, graph arrows, `@@`) better than fixed ones.
- **Trade-off:** Examples are embedded at `ctx init`, so editing the file needs a re-init. Every example was run against the real database before being committed.

### 7.3 Structured output for the generated query

- **Decision:** The model returns `{query, explanation}` through structured outputs (`output_config.format` with a JSON schema).
- **Why:** No parsing of code fences, and no prose around the query.
- **Trade-off:** None meaningful.

### 7.4 Three guardrails, in depth

- **Decision:**
  1. A static check: one `SELECT`, no write keywords, no `http::`/`crypto::`, and none of the excluded tables.
  2. Execution as a VIEWER database login, which can read but not write.
  3. A `TIMEOUT` appended to every query.
- **Why:** The static check is easy to slip past; the VIEWER role is the real write guardrail, enforced by the database. The excluded-tables check was added after the audit showed the VIEWER can read password hashes and every user's memories: hiding a table from the prompt doesn't stop the model querying it.
- **Trade-off:** A keyword blocklist can reject a legitimate query that uses a blocked word as a field name. Signed-in users run generated queries as themselves, so their permissions apply too.

### 7.5 `TIMEOUT`, not a wrapping `LIMIT`

- **Decision:** Row counts are capped in Python after the query; the query only gets `TIMEOUT 2s` appended.
- **Why:** Wrapping a query (`SELECT * FROM (query) LIMIT 50`) changed the result shape for `SELECT VALUE ... FROM ONLY`. `TIMEOUT` is always the last clause, so appending it is safe.
- **Trade-off:** A query can return many rows to the client before they're cut. The timeout bounds it.

### 7.6 Errors go back to the model, up to 3 attempts

- **Decision:** A guardrail rejection or a SurrealQL error is added to the next prompt as a `<failed_attempt>`.
- **Why:** Most failures are fixable typos (`documents` instead of `document`). Tested with a scripted LLM.
- **Trade-off:** Up to three LLM calls per question.

### 7.7 Record users are provisioned, never self-registered

- **Decision:** `DEFINE ACCESS account` has `SIGNIN` but no `SIGNUP`. `create_user()` (admin only) sets teams.
- **Why:** If users signed up themselves, they could choose their own `teams` and read any team's documents.
- **Trade-off:** An admin step per user (`ctx users` for the demo).

### 7.8 Permissions live in the schema, not in Python

- **Decision:** `document`, `chunk` and `links_to` have `PERMISSIONS FOR select WHERE visibility = 'public' OR team IN $auth.teams OR owner = $auth.id`. Writes are `NONE` for record users.
- **Why:** Every query a user runs, including vector and full-text search, is filtered the same way, with no application code to forget. This is the "agent as aggregator" lesson: the agent never sees what the user can't.
- **Trade-off:** Permissions are evaluated per row, and interact with KNN (4.5).

### 7.9 A stable VIEWER password from settings

- **Decision:** `ensure_viewer()` uses `CL_VIEWER_PASS` when set, and a random password otherwise.
- **Why:** Tested: redefining the password doesn't end open sessions, but a process that signs in after another one changed it is refused. A shared password removes that race. (Audit finding, fixed.)
- **Trade-off:** One more setting. Unset, behaviour is as before.

---

## 8. The agent (`agent.py`, `llm.py`)

### 8.1 A hand-written tool loop

- **Decision:** `ContextAgent.send()` loops: call Claude → run the requested tools → send the results back → repeat until no more tool calls (max 8 steps).
- **Why:** In a course, every step should be visible. The SDK's tool runner would hide the loop the lesson is about.
- **Trade-off:** We handle details the runner would (parallel results in one message, errors as `is_error`). Tests cover both.

### 8.2 Four tools

- **Decision:** `search_knowledge`, `query_database`, `recall_memory`, `remember`.
- **Why:** They map onto the course's lessons: document retrieval, structural questions, memory read, memory write. Each tool description says when to use it.
- **Trade-off:** No tool for editing documents; the agent is read-only over the corpus by design.

### 8.3 Memories attached to each user message

- **Decision:** The top 3 memories (without reinforcement) are prepended to the user message, not added to the system prompt.
- **Why:** Changing the system prompt each turn would break prompt caching. Adding to the message keeps the history append-only.
- **Trade-off:** Memories appear in the transcript once per turn.

### 8.4 An `LLM` protocol as the provider seam

- **Decision:** `llm.LLM` lists the three methods the context layer needs: `create`, `text`, `json`. `ClaudeLLM` and the tests' `ScriptedLLM` implement it.
- **Why:** The tests run the agent and text-to-SurrealQL with no network. Swapping providers means implementing three methods.
- **Trade-off:** None; the protocol was unused at first and is now the type everywhere an LLM is passed.

---

## 9. SurrealDB 3.2 behaviours we designed around

| # | Behaviour | What we did |
|---|---|---|
| 9.1 | `@@` matches only text with every query term | `@1,OR@` (4.2) |
| 9.2 | A filter in the same `WHERE` as KNN can return far fewer than k rows | Search unfiltered, then filter (4.3) |
| 9.3 | The KNN operator rejects parameters | `int()`-formatted literals (4.6) |
| 9.4 | `DELETE` then `INSERT` of the same ID in a transaction fails | Upsert in place, delete only stale rows (3.3) |
| 9.5 | `NONE <= time::now()` is true | `expires_at != NONE AND ...` in `forget_expired()` |
| 9.6 | `UPSERT ... CONTENT` drops `DEFAULT` values | `VALUE $before OR time::now()` (3.4) |
| 9.7 | Tied rows come back in no fixed order | `, id` on every ranking (4.9) |
| 9.8 | Permissions apply after the HNSW candidates are picked | Over-fetch; documented shortcut (4.5) |
| 9.9 | `SELECT VALUE count() ... GROUP ALL` returns `{count: n}` objects | Tests compare against that shape |
| 9.10 | `$session` is a reserved variable | The turn query uses `$session_id` |
| 9.11 | The SDK's `query()` returns only the first statement | `query_raw()` wrapper (2.1) |
| 9.12 | The SDK's embedded engine is 2.x | Real 3.x server, also in tests (1.2) |

---

## 10. Evaluation and testing

### 10.1 A hand-labelled golden set, tagged by question type

- **Decision:** 24 questions in `data/golden.json`, each with the documents that answer it and a tag: lexical, semantic, multihop or simple.
- **Why:** Without labels there's no way to tell whether a change helped. Tags show where each retriever wins rather than one averaged number.
- **Trade-off:** 24 questions is small; one question moves recall by about 4 points. The set includes one question nothing solves, kept on purpose so learners see why you measure rather than tune to examples.

### 10.2 Metrics at the document level

- **Decision:** recall@k, precision@k and MRR are computed on documents, after collapsing chunks to their first-ranked document.
- **Why:** People judge "did it find the right page", and chunk boundaries differ between strategies, so chunk-level labels wouldn't compare across them.
- **Trade-off:** A retriever that finds the right page but the wrong section still scores full marks.

### 10.3 Metrics in Python and in SurrealQL

- **Decision:** `fn::recall_at_k`, `fn::precision_at_k` and `fn::reciprocal_rank` exist in the schema, and `evaluate(..., db=db)` uses them; a test checks both implementations agree.
- **Why:** The SurrealDB course teaches metrics as database functions. The Python versions are easier to read and debug.
- **Trade-off:** Two implementations, kept honest by the test.

### 10.4 Tests against a real database, with mutation checks

- **Decision:** Tests start a throwaway `surreal` server and use a fresh database per test. New tests were checked by breaking the fix they cover and confirming they fail.
- **Why:** The behaviour under test lives in SurrealQL; mocking the database would test nothing. Several first-draft tests passed even with the fix removed, so they were rewritten.
- **Trade-off:** Tests need the `surreal` binary (they skip without it).

### 10.5 A fictional company as the sample corpus

- **Decision:** 20 pages from "Lumen Pay", a made-up payments company: services, runbooks, policies, incidents, three team-only pages.
- **Why:** Designed to exercise every lesson: error codes for keyword search, paraphrases for vector search, incidents whose answer is two links away for graph search, team pages for permissions. Fictional, so nothing real is published.
- **Trade-off:** Real wikis are messier. Learners are encouraged to ingest their own.

---

## 11. Project and tooling

### 11.1 A private GitHub repository on `main`

- **Decision:** The code lives in a private repo (`nottanishqq/context-layer`). The branch was renamed from `master` to `main` before the first push.
- **Why:** The user asked for a private repo. `main` is GitHub's default and what most tooling expects.
- **Trade-off:** None.

### 11.2 Demo passwords committed

- **Decision:** `data/users.json` holds made-up demo passwords and is in the repo.
- **Why:** They are fake, already shown in the README, and needed for the permissions lesson.
- **Trade-off:** If the repo is ever made public, these must stay obviously demo-only. Real secrets go in `.env`, which is git-ignored.

### 11.3 Knowledge graph built locally, not from a downloaded viewer

- **Decision:** The understand-anything dashboard runs from the locally installed plugin, not from the prebuilt viewer the plugin downloads from GitHub.
- **Why:** Running a freshly downloaded package wasn't needed when the local copy works.
- **Trade-off:** None.
