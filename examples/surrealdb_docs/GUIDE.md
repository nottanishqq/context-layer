# Context layer over the SurrealDB docs

This folder points the context layer at the real SurrealDB documentation, so you can ask it questions like:

> Question about upgrading. We're currently on v2.6.2 and want to upgrade to the latest, and we have about 5–10 instances. What is the best way to upgrade? Should we go straight 2.6.2 → 3.0.0 and then to the latest version, or from 2.6.2 to the latest version?

It is not part of the course. Everything lives in `examples/surrealdb_docs/`, and nothing in `src/` was changed to support it. To remove it, follow [Removing it](#removing-it).

| File | What it does |
|---|---|
| `prepare.py` | Downloads the docs and converts them into one Markdown file per page, in `pages/` |
| `ask.py` | Answers a question: retrieves context with the library, then asks Claude with a SurrealDB support prompt |
| `test_prepare.py` | Tests for the converter |
| `GUIDE.md` | This file |

---

## How it works

SurrealDB publishes its whole documentation as one Markdown file, [`llms-full.txt`](https://surrealdb.com/docs/llms-full.txt) (about 8 MB, 1,051 pages). Each page starts with a `Source: <url>` line. `prepare.py`:

1. splits that file into one Markdown file per page;
2. gives each page front matter the library's ingestion understands: `title`, `kind` (the docs section, such as `build` or `reference`), `tags` and the page `url`;
3. rewrites links between pages (`[text](/docs/x/y.md)`) as `[[x-y|text]]` wikilinks. Ingestion turns those into `links_to` edges, so graph expansion can follow them. That matters for upgrade questions: the overview page links to the version-specific guides;
4. adds the release notes index ([`releases.md`](https://surrealdb.com/releases.md)), which is how "the latest version" gets answered. The docs pages themselves don't say which release is current.

From there it's the normal pipeline: `ctx ingest` chunks and embeds the pages, and `ctx search` / `ctx context` show what retrieval finds.

`ask.py` adds one step on top of the library, **query planning**:

1. Claude rewrites the question as 3–5 search queries in documentation vocabulary.
2. The library's `ContextBuilder` retrieves for each query (hybrid search plus one hop of graph expansion), and the results are merged round-robin into one context.
3. Claude answers from that context, citing sources.

Planning is needed because real questions don't use the words of the pages that answer them. The upgrade question never says "export", "diagnostics" or "migration", and a single search for it missed the sections with the actual migration commands (see [What the test run found](#what-the-test-run-found)).

---

## Steps from scratch

### 1. Install the prerequisites

You need:

- **Python 3.13+** and **[uv](https://docs.astral.sh/uv/)**.
- **The `surreal` binary, version 3.x.** macOS and Linux:

  ```bash
  curl -sSf https://install.surrealdb.com | sh
  ```

  Windows (PowerShell):

  ```powershell
  iwr https://windows.surrealdb.com -useb | iex
  ```

  Check it with `surreal version`.
- **An Anthropic API key**, for the final answering step only. Everything up to step 8 works without one.

### 2. Get the code

```bash
git clone https://github.com/nottanishqq/context-layer.git
cd context-layer
uv sync
```

`uv sync` creates the virtual environment and installs the dependencies, including the local embedding model library.

### 3. Start SurrealDB with on-disk storage

In a separate terminal, which you leave running:

```bash
surreal start --user root --pass root surrealkv://surrealdb-docs.db
```

Use `surrealkv://` rather than `memory`: ingesting the docs takes several minutes, and in-memory data is lost when the server stops.

### 4. Configure

```bash
cp .env.example .env
```

Then edit `.env` and set `ANTHROPIC_API_KEY=...`. The defaults for everything else work with the server from step 3.

### 5. Download and convert the docs

```bash
uv run python examples/surrealdb_docs/prepare.py
```

Expected output: `Wrote 1052 pages to .../examples/surrealdb_docs/pages`. The folder is git-ignored.

If you've already downloaded `llms-full.txt`, pass `--from path/to/llms-full.txt`. The release notes are always fetched fresh.

### 6. Create the database

Use a separate database, `surrealdb_docs`, so the docs never mix with the course's sample corpus:

```bash
uv run ctx --db surrealdb_docs init
```

This applies the schema and records the embedding model in the database. It also loads the course's text-to-SurrealQL examples, which don't matter here.

### 7. Ingest

```bash
uv run ctx --db surrealdb_docs ingest examples/surrealdb_docs/pages
```

This chunks every page by section, embeds each chunk with the local bge-small model, and links the pages. Expect about 12 minutes on a laptop CPU, almost all of it embedding. The output ends with:

```
Ingested 1052 documents -> 10881 chunks (section), 5061 links; ...
```

### 8. Check retrieval (no API key needed)

See what each retriever finds:

```bash
uv run ctx --db surrealdb_docs search "upgrade from 2.6.2 to the latest version, 2.x to 3.x, several instances" -k 8
```

See the exact context Claude would get:

```bash
uv run ctx --db surrealdb_docs context "We are on v2.6.2 and want to upgrade to the latest version across 5-10 instances. Should we go 2.6.2 -> 3.0.0 -> latest, or straight to the latest?" -k 8
```

Those two commands run a single search. To check the multi-query retrieval `ask.py` uses, still without an API key, give it your own sub-queries:

```bash
uv run python examples/surrealdb_docs/ask.py "We are on v2.6.2 and want to upgrade to the latest version. We have 5-10 instances. Should we go 2.6.2 -> 3.0.0 -> latest, or straight from 2.6.2 to the latest?" --context-only --queries "upgrade SurrealDB from 2.x to 3.x" "export a 2.x database in a v3 compatible format and import it into 3.x" "migration diagnostics minimum version" "latest stable SurrealDB release" "automatic data migrations when upgrading to 3.3" "upgrading several instances or a cluster, backups and rolling upgrades"
```

For the upgrade question, the context should include:

- the "2.x to 3.x" guide, including its "Using the CLI" section;
- the general "Upgrading" overview;
- "Upgrades & patching";
- "3.2 to 3.3";
- the release notes.

If it doesn't, see [When answers are poor](#when-answers-are-poor) before involving the model.

### 9. Ask

```bash
uv run python examples/surrealdb_docs/ask.py "We are on v2.6.2 and want to upgrade to the latest version. We have 5-10 instances. Should we go 2.6.2 -> 3.0.0 -> latest, or straight from 2.6.2 to the latest?"
```

It prints the queries Claude chose, then the answer, then the sources with their URLs. Sources the answer actually cited are marked with `*`.

Options:

- `-k` sets the hits per query (default 5).
- `--budget` sets the context size in tokens (default 8000).
- `--queries` replaces the planning step with your own queries.

### 10. Keep it current

The docs change often. To refresh, re-run steps 5 and 7. Ingestion updates changed pages, adds new ones, and removes pages that no longer exist.

---

## What a good answer to the upgrade question contains

Use this to judge the output. Everything below comes from the docs pages the context should include:

- **Don't stop at 3.0.0.** Going from 2.x to 3.x is a data migration, not a binary swap: a 3.x binary can't read 2.x data directly. You export the 2.x database in a v3-compatible format and import it into 3.x. The 2.x to 3.x guide targets "the latest 3.x release", so export from 2.6.2 and import straight into the latest version. ([2.x to 3.x](https://surrealdb.com/docs/build/migrating/from-old-surrealdb-versions/2x-to-3x))
- **2.6.2 is new enough for the migration tooling.** The migration diagnostics in SurrealDB Studio need 2.6.1 or later. They list what to fix before exporting.
- **The command-line route** uses the 3.x binary (3.0.3 or later):
  - `surreal v2 export --v3 ...` exports from the 2.x database;
  - `surreal db import ...` imports into the 3.x instance.
- **The latest stable release** is given by the release notes ([releases](https://surrealdb.com/releases)). At the time of the test run that was 3.3, latest patch 3.3.4 (13 Oct 2026).
- **Starting fresh on the latest 3.x has an advantage.** A datastore created on 3.3.0 or later records every data migration as already applied. That sidesteps the 3.2 → 3.3 migration notes. ([Upgrades & patching](https://surrealdb.com/docs/manage/self-hosted/upgrades-and-patching))
- **With 5–10 instances:**
  - take a fresh backup (`surreal db export`) of each instance before you start;
  - validate the export and import on staging with production-like data;
  - then migrate the instances one at a time.
  - For clustered setups, upgrade one node at a time and check health between nodes.
  - Reverting needs the pre-upgrade export.

---

## What the test run found

The full pipeline was run on 2026-10-20 against the live docs: 1,052 pages and SurrealDB 3.2.3. The Claude steps weren't run, because there was no API key on the build machine. So retrieval is verified; the planning step and the final answer are not.

**Ingestion:**

- 709 seconds in total.
- 1,052 pages became 10,881 section chunks and 5,061 links between pages.
- Embedding was the bottleneck at about 10–15 chunks per second. Database writes took under 0.1 s per page.

**Retrieval for the upgrade question.** Seven facts a good answer needs (the list above) were checked against the context sent to Claude:

| Retrieval | Facts in context | Missing |
|---|---|---|
| Single search (`ctx context`), k=8 | 3 / 7 | the `surreal v2 export --v3` command, the 2.6.1 requirement, the `surreal db import` step, the 3.3 automatic-migration note |
| Single search, tuned: k=12–16, more chunks per linked page | 3–4 / 7 | the same, mostly |
| Six sub-queries (`--queries` above), k=5, one graph hop | 7 / 7 | none |

What that shows:

- **Single search falls short.** It finds the right pages: graph expansion reaches "2.x to 3.x" through links from the overview. But it only brings each page's opening chunks, not the sections with the commands. No tuning fixed that.
- **Targeted queries reach the right sections.** "Export a 2.x database in a v3 compatible format" finds the CLI section, and "migration diagnostics minimum version" finds the 2.6.1 note.
- **The sub-queries above were written by hand** as a stand-in for what Claude's planning step should produce. Whether Claude writes queries as good is the untested part. `ask.py` prints the queries it used, so check them when an answer misses something.
- **Some noise comes with it.** The "diagnostics" query also pulls in the JavaScript and PHP SDKs' error-handling pages, which use the same word.

---

## When answers are poor

Work from retrieval outward. Fix what `ctx context` shows before touching the prompt.

- **The right page isn't retrieved.** Try `--strategy parent_child` at both ingest and query time. Small child chunks match specific questions better, and the parent section is still what Claude reads. Re-ingest into a fresh database (`ctx --db surrealdb_docs reset`, then steps 6–7 with `--strategy parent_child`), because the library keeps one strategy per database.
- **Reference pages crowd out guides.** The docs have about 680 reference pages (functions, statements, CLI flags). In code, `Retriever.hybrid(..., kinds=["build", "manage"])` restricts a search to the guide sections. `kind` is the first part of the page's URL path.
- **The planned queries miss a part of the question.** Look at the "Searched for" lines `ask.py` prints. Either add your own with `--queries`, or extend the `PLANNER` prompt in `ask.py` with the kind of page your users' questions need.
- **The answer is cut short or misses a page.** Raise `-k` (hits per query) or `--budget` (default 8,000 tokens).
- **Measure before tuning.** Write a small golden set in the format of `data/golden.json`: questions plus the page slugs that answer them, where a slug is the file name in `pages/` without `.md`. Then compare retrievers with `evaluate()` from `contextlayer.evaluation`. Don't use `ctx eval` here: it re-ingests the corpus once per chunking strategy, which is slow on 1,000 pages.

---

## Limits

- **Current docs only.** The docs describe the current release. Questions about how an old version behaved may get today's answer. The version-specific upgrade guides are the exception.
- **Release notes depth.** Only the release notes index is ingested, not each release's detailed notes. Adding them is one more `fetch()` per page in `prepare.py`, for example `https://surrealdb.com/releases/3.3.md`.
- **`ask.py` is one-shot.** It doesn't keep a conversation, remember the user, or run SurrealQL. It makes two Claude calls per question: one to plan, one to answer. `ctx chat` does all of that, but its system prompt is written for the course's sample company.
- **Raw components.** Some documentation components (`<OptionsTable>`, `<Synopsis>`) arrive as raw JSX in `llms-full.txt`. They're left as they are; their content is readable text to both search and the model.

---

## Removing it

```bash
uv run ctx --db surrealdb_docs reset
```

Then stop the server, delete the `surrealdb-docs.db` storage folder, and delete `examples/`. Nothing else in the repository depends on this folder.
