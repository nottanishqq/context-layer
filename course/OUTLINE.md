# Course outline: Build a context layer for AI agents with SurrealDB and Python

A follow-along project course, in text. You build one working system from an empty folder to a deployed-quality context layer: an assistant that answers questions from a company's internal wiki, remembers who it's talking to, respects who may see what, and can prove with numbers that each change made it better.

There is no lesson on "vector search" or "BM25". Every milestone starts with something the assistant gets wrong, and you build whatever fixes it. The concepts arrive when the project needs them.

---

## Who it's for

- Developers who have called an LLM API and want to build the part underneath it: what the model gets to read.
- Comfortable with Python. No prior SurrealDB experience needed; each SurrealQL feature is introduced where the project first needs it.

## What you need

- Python 3.13+ and [uv](https://docs.astral.sh/uv/).
- The `surreal` binary, version 3.x.
- An Anthropic API key, from milestone 3 onwards. Everything before that, and every evaluation, runs without one.

---

## How a text course works here

Every milestone is one chapter, written as Markdown, with the same shape:

1. **The problem.** A question from Lumen Pay's engineers that the assistant gets wrong today, shown with the real command and its real output.
2. **The failing check.** A golden-set question, or a test, that fails because of it. You add it before writing any code.
3. **Build.** The code, introduced in small pieces. SurrealQL is shown on its own first and run in `surreal sql`, then wrapped in Python.
4. **Check.** Commands to run, with the output you should see. If yours differs, the chapter says what usually causes it.
5. **Measure.** `ctx eval` before and after, so you see the change in recall and MRR, and where it got worse.
6. **Why it's built this way.** The decision, the alternatives and the trade-off, in short sidebars. (Long form: `decisions.md`.)
7. **Exercises.** Two or three, each with a solution in the repository.

**The repository is the backbone.** Each milestone is a git tag (`m0` … `m9`, `capstone`). Lost? `git checkout m4` puts you at the end of milestone 4. `git diff m3 m4` shows exactly what the chapter changed. Every chapter can be started from its previous tag, so readers can skip ahead.

**Everything is real.** Real embeddings from a local model (`fastembed`, no API key), a real SurrealDB server, real failures. Where SurrealDB behaves unexpectedly, the chapter shows it happening, then the workaround.

---

## The story

You join **Lumen Pay**, a fictional payments company, and are asked to build an assistant over its internal wiki: 20 pages of services, runbooks, policies and incident reports. Three pages are team-only. The questions come from its engineers, and they get harder as the assistant gets better.

---

## Milestones

### M0 — A database and a skeleton

**Problem:** nothing exists yet.

**You build:**

- A project with uv.
- A local SurrealDB 3 server.
- `ctx init`, which applies a schema of documents and chunks.

**You learn:**

- SCHEMAFULL tables, and running SurrealQL from `surreal sql` and from Python.
- Why the Python SDK's `query()` isn't enough for multi-statement queries.

**Ends with:** `ctx init` succeeds, and `INFO FOR DB` shows the schema.

### M1 — "Search the wiki"

**Problem:** an engineer asks "how long are application logs kept?" and there is nothing to search.

**You build:**

- Ingestion: Markdown with front matter, split into section chunks and embedded locally.
- Vector search: `ctx search`.
- The golden set, from day one: five questions with the pages that answer them, and `ctx eval`.

**You learn:**

- Embeddings, HNSW indexes and the `<|k,ef|>` operator.
- Exact versus approximate search: you measure that the index agrees with a full scan.
- Deterministic record IDs, and re-ingesting safely in a transaction.

**Ends with:** the first recall@5 number. Every later milestone moves it.

### M2 — "It can't find `ERR_LEDGER_409`"

**Problem:** vector search returns pages that are about errors, not the page that defines this one.

**You build:**

- A full-text index with BM25.
- Hybrid search that fuses both result lists with Reciprocal Rank Fusion: first by hand, then with `search::rrf`.

**You learn:**

- Analyzers, and why plain `@@` matches nothing for natural-language questions (`@1,OR@`).
- Why fusion uses ranks, not scores.
- The filtered-KNN pitfall, shown failing first: search unfiltered, then filter.

**Ends with:** keyword search alone gets every lexical question (recall 1.0 against vector search's 0.917), and hybrid has the best MRR so far.

### M3 — "Answer the question, and show your sources"

**Problem:** search results aren't answers.

**You build:** `ctx context` and `ctx ask`, covering:

- relevance thresholds;
- a token budget;
- numbered sources;
- a Claude call that cites them.

**You learn:**

- What context assembly decides, and why it's printed before any model sees it.
- Why commands in sources must reach the model unescaped.

**Ends with:** an answer with `[1]`-style citations, and the CLI marks which sources were cited.

### M4 — "The answer is two links away"

**Problem:** "EU customers get confirmation emails hours late — how do we fix it?" Retrieval finds the notification service, but the fix is in a runbook two links away that never mentions email.

**You build:** `[[wikilinks]]` as graph edges, and graph expansion over them, with per-source pruning.

**You learn:**

- Graph traversal in SurrealQL: `->links_to->`, `.{1..2+collect}` and `+shortest`.
- How pruning globally threw away the only bridge to the answer, and how per-source pruning fixes it.

**Ends with:** multi-hop recall up (0.667 to 0.733), and semantic recall down (1.0 to 0.857), because graph hits push other results out of the top 5. You decide whether the trade is worth it.

### M5 — "It forgot who I am" / "I changed teams"

**Problem:** the assistant asks the same questions every conversation. When Pat changes teams, it remembers both teams.

**You build:**

- Agent memory with recall that blends similarity, recency and importance, plus reinforcement and expiry.
- Corrections that supersede instead of overwrite, and history you can query.

**You learn:**

- Why a vector store that just appends returns both the old fact and the new one.
- Two clocks: what was true, and what we believed.
- Exact scans versus indexes for small filtered sets.
- Provenance back to the conversation turn.

**Ends with:** `ctx memory lookup user team --as-of 2026-06-01` answers correctly for the past and the present.

### M6 — "Pat can see the security findings"

**Problem:** the assistant happily quotes team-only pages to anyone.

**You build:**

- Record users and team-based permissions in the schema.
- Search that runs as the signed-in user.

**You learn:**

- `DEFINE ACCESS`, `$auth` and table `PERMISSIONS`.
- Why filtering inside the database beats filtering in the agent.
- How permissions interact with vector search candidates.

**Ends with:** the same search returns different results for Pat and Sam, and a test proves it.

### M7 — "How many runbooks do we have?"

**Problem:** counting, listing and "what links to X" aren't similarity questions.

**You build:** text-to-SurrealQL, with three parts:

- a schema prompt generated from the database;
- examples chosen by similarity;
- retries that feed the error back to the model.

**You learn:**

- Guardrails in layers: static checks, then a read-only VIEWER login, then timeouts.
- Why hiding a table from the prompt doesn't protect it.

**Ends with:** `ctx sql` answers structural questions, and the guardrail tests pass.

### M8 — "Make it an assistant"

**Problem:** users have to know which command to run.

**You build:** a Claude agent with four tools (search, query, recall, remember) and a hand-written tool loop.

**You learn:**

- Tool use, sending tool errors back to the model, and parallel tool results.
- Attaching memories without breaking prompt caching.

**Ends with:** `ctx chat` that searches, counts, remembers and corrects itself, across turns.

### M9 — "Ship it"

**Problem:** it works on your laptop, once.

**You build:**

- Chunking strategies, compared on the golden set.
- Deterministic rankings (tie-breaks).
- A guard against switching embedding models.
- Re-ingest with pruning, and duplicate-slug detection.
- Tests that run against a throwaway server.

**You learn:**

- Measuring instead of tuning to examples. One question in the golden set is never solved, on purpose.
- What breaks at scale, marked in the code as `shortcut:` comments.

**Ends with:** a green test suite, and an evaluation table that prints the same numbers on every run.

### Capstone — your own docs

Point the finished system at a real documentation set. The worked example is SurrealDB's own docs: 1,052 pages, about 11,000 chunks and 5,000 links. You:

- write a converter for their format;
- add a query-planning step for multi-part questions;
- check retrieval against a list of facts a good answer must contain.

Then do the same for a documentation set of your choice.

---

## Running threads

- **The golden set grows with the project.** Each milestone adds the questions it fixes, tagged lexical, semantic, multi-hop or simple. By M9 there are 24, and the evaluation table shows which milestone moved which column.
- **One question is carried through:** "EU customers get confirmation emails hours late." Vector search finds the wrong half, hybrid search keeps it on top, and graph expansion finally reaches the runbook.
- **One question stays unsolved:** "Merchants see payouts that haven't shown up in their balance yet." It's a reminder that you measure the whole set instead of chasing one example.
- **The decision log.** Each chapter ends by adding its decisions to `decisions.md`, the way a team would.

---

## Producing the course

- **Milestone tags.** Cut the finished repository back into `m0` … `m9` and `capstone` tags. Each tag must pass its own tests.
- **Real numbers per milestone.** The figures quoted above come from the finished code's evaluation. Each chapter's "before and after" numbers must be re-measured at its own tag.
- **Pin versions.** Record the exact SurrealDB, Python and package versions each chapter was tested with. Re-run the suite on every SurrealDB minor release before publishing an update: the code was built on 3.2.3, and 3.3 is out.
- **Keep it original.** The SurrealDB University course covers some of the same concepts. Don't reuse its text, structure, diagrams or exercises.
- **Third-party content.** Don't bundle SurrealDB's documentation in course materials. The capstone downloads it on the learner's machine.

## Open decisions

- **Where it's published.** Udemy requires at least 30 minutes of video across 5 or more lectures for a paid course, so a text-only course can't go there as is. Options:
  - a text-first platform (your own site, Leanpub, Gumroad, or Teachable with text lessons);
  - free on GitHub;
  - Udemy with short companion videos added to the text chapters.
- **Exercise solutions.** A `solutions` branch, or a `solutions/` folder per milestone.
- **Claude cost for learners.** Whether to offer a cheaper model option (Sonnet or Haiku) in `.env.example` for M3, M7 and M8.
