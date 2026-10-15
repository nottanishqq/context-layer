"""`ctx`: a command line for the context layer.

    ctx init                       apply the schema, load text-to-SurrealQL examples
    ctx ingest [DIR]               ingest Markdown documents
    ctx search "question"          show what each retriever returns
    ctx context "question"         show the exact context the model would receive
    ctx ask "question"             answer with retrieval-augmented generation (needs Claude)
    ctx sql "question"             text-to-SurrealQL (needs Claude)
    ctx chat                       talk to the agent (needs Claude)
    ctx memory ...                 remember, recall, correct and inspect agent memory
    ctx users                      create the demo users from data/users.json
    ctx eval                       compare retrievers and chunking strategies on the golden set
    ctx reset                      delete the database
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from contextlayer.config import Settings
from contextlayer.db import Database

# The repository root (src/contextlayer/cli.py -> ../..), so `ctx` finds data/
# from any working directory. Assumes the editable install that `uv sync` makes.
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


def _settings(args: argparse.Namespace, **overrides) -> Settings:
    base = Settings()
    changes = {k: v for k, v in {"surreal_db": args.db, "surreal_ns": args.ns, "embedder": args.embedder}.items() if v}
    return dataclasses.replace(base, **{**changes, **overrides})


def _embedder(settings: Settings):
    from contextlayer.embeddings import get_embedder

    return get_embedder(settings.embedder, settings.embedding_model)


def _connect(args: argparse.Namespace, settings: Settings | None = None) -> Database:
    """Root connection, or a record user's if --user/--password were given."""
    settings = settings or _settings(args)
    if getattr(args, "user", None):
        from contextlayer.access import connect_as_user

        return connect_as_user(args.user, args.password, settings)
    return Database.connect(settings)


def _short(doc_id: str) -> str:
    return doc_id.split(":", 1)[1]


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> None:
    settings = _settings(args)
    emb = _embedder(settings)
    with Database.connect(settings) as db:
        db.query(f"DEFINE NAMESPACE IF NOT EXISTS {settings.surreal_ns}; DEFINE DATABASE IF NOT EXISTS {settings.surreal_db};")
        db.apply_schema(emb)
        from contextlayer.text2surql import TextToSurql

        n = TextToSurql(db, None, emb).load_examples(DATA / "surql_examples.json")
    print(f"Schema applied to {settings.surreal_ns}/{settings.surreal_db} (embedding dimension {emb.dim}); "
          f"{n} SurrealQL examples loaded.")


def cmd_ingest(args: argparse.Namespace) -> None:
    from contextlayer.ingest import Ingestor, load_directory

    settings = _settings(args)
    with Database.connect(settings) as db:
        docs = load_directory(args.path)
        # The directory is the source of truth: documents whose files are gone are removed.
        stats = Ingestor(db, _embedder(settings), [args.strategy]).ingest_all(docs, prune=not args.keep)
    print(f"Ingested {stats['documents']} documents -> {stats['chunks']} chunks ({args.strategy}), "
          f"{stats['links']} links; removed {stats['removed']} documents no longer in {args.path}.")


def _retriever(args: argparse.Namespace, db: Database):
    from contextlayer.retrieval import Retriever

    return Retriever(db, _embedder(db.settings), strategy=args.strategy)


def cmd_search(args: argparse.Namespace) -> None:
    with _connect(args) as db:
        r = _retriever(args, db)
        modes = ["vector", "fulltext", "hybrid", "hybrid_graph"] if args.mode == "all" else [args.mode]
        for mode in modes:
            print(f"\n== {mode}")
            for i, h in enumerate(getattr(r, mode)(args.question, args.k), 1):
                extra = f"  via {_short(h.signals['via'])}" if "via" in h.signals else ""
                print(f"{i:>2}. {h.score:8.4f}  {_short(h.chunk_id):<45} {h.title}{extra}")
                if args.verbose:
                    print("      " + h.text.replace("\n", " ")[:300])


def _builder(args: argparse.Namespace, db: Database):
    from contextlayer.context import ContextBuilder

    return ContextBuilder(_retriever(args, db), _memory_store(args, db), k=args.k, expand_graph=not args.no_graph)


def cmd_context(args: argparse.Namespace) -> None:
    with _connect(args) as db:
        packet = _builder(args, db).build(args.question)
        print(packet.render())
        print(f"\n-- {len(packet.sources)} sources, {len(packet.memories)} memories, ~{packet.tokens} tokens; "
              f"{len(packet.dropped)} hits dropped")


def cmd_ask(args: argparse.Namespace) -> None:
    from contextlayer.context import answer
    from contextlayer.llm import ClaudeLLM

    with _connect(args) as db:
        result = answer(args.question, _builder(args, db), ClaudeLLM())
    print(result.text)
    print("\nSources:")
    for s in result.context.sources:
        mark = "*" if s in result.cited else " "
        print(f" {mark}[{s.index}] {s.title} ({_short(s.document_id)})")


def _text_to_surql(args: argparse.Namespace, db: Database, llm):
    """Text-to-SurrealQL that executes as the signed-in user, or as a VIEWER for root."""
    from contextlayer.access import ensure_viewer
    from contextlayer.text2surql import TextToSurql

    admin = Database.connect(db.settings)
    executor = db if getattr(args, "user", None) else Database.connect_database_user(
        *ensure_viewer(admin), settings=db.settings)
    return TextToSurql(admin, llm, _embedder(db.settings), executor=executor)


def cmd_sql(args: argparse.Namespace) -> None:
    from contextlayer.llm import ClaudeLLM

    with _connect(args) as db:
        result = _text_to_surql(args, db, ClaudeLLM()).ask(args.question)
    for query, error in result.attempts:
        print(f"-- failed: {query}\n--   {error}")
    if not result.ok:
        sys.exit("Could not produce a valid query.")
    print(f"-- {result.explanation}\n{result.query}\n")
    print(json.dumps(result.rows, indent=2, default=str))


def cmd_chat(args: argparse.Namespace) -> None:
    from contextlayer.agent import ContextAgent
    from contextlayer.llm import ClaudeLLM

    llm = ClaudeLLM()
    with _connect(args) as db:
        builder = _builder(args, db)
        from contextlayer.memory import llm_reconciler

        builder.memory.reconciler = llm_reconciler(llm)  # judge corrections that arrive without a slot

        def show(call):
            arg = next(iter(call.input.values()), "")
            print(f"  [{call.name}] {str(arg)[:80]}")

        agent = ContextAgent(llm, builder, builder.memory, _text_to_surql(args, db, llm), on_tool=show)
        print("Chat with the Lumen Pay assistant. Empty line to exit.")
        while message := input("\nyou> ").strip():
            print(f"\nassistant> {agent.send(message).text}")


def _memory_store(args: argparse.Namespace, db: Database):
    """A record user's memories are their own; root's go to an ownerless store."""
    from contextlayer.db import rid_str
    from contextlayer.memory import MemoryStore

    owner = db.one("RETURN $auth.id") if getattr(args, "user", None) else None
    return MemoryStore(db, _embedder(db.settings), agent=getattr(args, "agent", "assistant"),
                       owner=rid_str(owner) if owner else None)


def _print_memory(m, *, show_time: bool = True) -> None:
    now = datetime.now(timezone.utc)
    status = "current" if m.valid_at(now) else "upcoming" if m.valid_from > now else "ended"
    slot = f" [{m.subject}.{m.attribute}]" if m.subject else ""
    span = f"  valid {m.valid_from:%Y-%m-%d %H:%M}" + (f" -> {m.valid_until:%Y-%m-%d %H:%M}" if m.valid_until else " ->")
    print(f"{m.id:<28} {status:<10} {m.kind:<10}{slot} {m.content}")
    if show_time:
        source = f"  source {m.source_turn}" if m.source_turn else ""
        print(f"{'':<28}{span}  recorded {m.created_at:%Y-%m-%d %H:%M}  confidence {m.confidence:.2f}{source}")


def cmd_memory(args: argparse.Namespace) -> None:
    with _connect(args) as db:
        store = _memory_store(args, db)
        if args.action == "remember":
            result = store.remember(args.text, kind=args.kind, importance=args.importance,
                                    subject=args.subject, attribute=args.attribute,
                                    valid_from=args.valid_from, replaces=args.replaces, ttl=args.ttl)
            extra = f" (supersedes {', '.join(result.superseded)})" if result.superseded else ""
            print(f"{result.action}: {result.id}{extra}")
        elif args.action == "recall":
            for m in store.recall(args.text, k=args.k, as_of=args.as_of, known_at=args.known_at):
                print(f"{m.score:.3f}  ", end="")
                _print_memory(m, show_time=False)
        elif args.action == "lookup":
            m = store.lookup(args.subject, args.attribute, as_of=args.as_of, known_at=args.known_at)
            _print_memory(m) if m else print("(nothing)")
        elif args.action == "history":
            for m in store.history(args.subject, args.attribute):
                _print_memory(m)
        elif args.action == "lineage":
            for m in store.lineage(args.id):
                _print_memory(m)
        elif args.action == "list":
            for m in store.all(include_superseded=args.all):
                _print_memory(m)
        elif args.action == "erase":
            print(f"Deleted {store.forget_user()} memories and conversation turns.")


def cmd_users(args: argparse.Namespace) -> None:
    from contextlayer.access import create_user

    users = json.loads((DATA / "users.json").read_text(encoding="utf-8"))
    with Database.connect(_settings(args)) as db:
        for u in users:
            print(f"{create_user(db, **u)}  {u['email']}  teams={u['teams']}")


def cmd_eval(args: argparse.Namespace) -> None:
    from contextlayer.evaluation import evaluate, format_reports, load_golden
    from contextlayer.ingest import Ingestor, load_directory
    from contextlayer.retrieval import Retriever, rank_documents

    golden = load_golden(args.golden)
    docs = load_directory(args.path)
    reports = []
    for strategy in args.strategies.split(","):
        # One strategy per database, so each index only holds what it is being judged on.
        settings = _settings(args, surreal_db=f"{_settings(args).surreal_db}_eval_{strategy}")
        emb = _embedder(settings)
        with Database.connect(settings) as db:
            db.query(f"REMOVE DATABASE IF EXISTS {settings.surreal_db}; DEFINE DATABASE {settings.surreal_db};")
        with Database.connect(settings) as db:
            db.apply_schema(emb)
            Ingestor(db, emb, [strategy]).ingest_all(docs)
            r = Retriever(db, emb, strategy=strategy)
            for mode in args.retrievers.split(","):
                search = getattr(r, mode)
                reports.append(evaluate(
                    f"{mode}/{strategy}",
                    lambda q: rank_documents(search(q, 3 * args.k))[: args.k],
                    golden, args.k,
                ))
            print(f"  evaluated {strategy}", file=sys.stderr)
    print(format_reports(reports))
    if args.misses:
        for report in reports:
            for item, got, scores in report.per_question:
                if scores.recall < 1:
                    print(f"[{report.name}] recall {scores.recall:.2f}  {item.question}\n"
                          f"    got:  {', '.join(_short(d) for d in got)}\n"
                          f"    want: {', '.join(_short(d) for d in item.relevant)}")


def cmd_reset(args: argparse.Namespace) -> None:
    settings = _settings(args)
    with Database.connect(settings) as db:
        db.query(f"REMOVE DATABASE IF EXISTS {settings.surreal_db}")
    print(f"Removed {settings.surreal_ns}/{settings.surreal_db}.")


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="ctx", description="A context layer for AI on SurrealDB.")
    parser.add_argument("--ns", help="namespace (default: $SURREAL_NS or contextlayer)")
    parser.add_argument("--db", help="database (default: $SURREAL_DB or main)")
    parser.add_argument("--embedder", choices=["fastembed", "hash"], help="default: $CL_EMBEDDER or fastembed")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, fn, help):
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        return p

    def as_user(p):
        p.add_argument("--user", help="sign in as this record user (email)")
        p.add_argument("--password", help="the record user's password")

    def retrieval_opts(p):
        p.add_argument("-k", type=int, default=5)
        p.add_argument("--strategy", default="section", choices=["section", "fixed", "sentence", "parent_child"])

    add("init", cmd_init, "apply the schema")

    p = add("ingest", cmd_ingest, "ingest a directory of Markdown files")
    p.add_argument("path", nargs="?", default=str(DATA / "corpus"))
    p.add_argument("--keep", action="store_true", help="keep documents whose files are gone")
    p.add_argument("--strategy", default="section", choices=["section", "fixed", "sentence", "parent_child"])

    p = add("search", cmd_search, "run the retrievers")
    p.add_argument("question")
    p.add_argument("--mode", default="all", choices=["all", "vector", "fulltext", "hybrid", "hybrid_graph"])
    p.add_argument("-v", "--verbose", action="store_true")
    retrieval_opts(p)
    as_user(p)

    for name, fn, help in [("context", cmd_context, "show the assembled context"),
                           ("ask", cmd_ask, "answer a question with RAG")]:
        p = add(name, fn, help)
        p.add_argument("question")
        p.add_argument("--no-graph", action="store_true", help="don't follow document links")
        retrieval_opts(p)
        as_user(p)

    p = add("sql", cmd_sql, "text-to-SurrealQL")
    p.add_argument("question")
    as_user(p)

    p = add("chat", cmd_chat, "chat with the agent")
    p.add_argument("--no-graph", action="store_true")
    retrieval_opts(p)
    as_user(p)

    p = add("memory", cmd_memory, "agent memory: remember, recall, history")
    p.add_argument("--agent", default="assistant")
    as_user(p)
    msub = p.add_subparsers(dest="action", required=True)
    m = msub.add_parser("remember", help="store a memory (supersedes the slot's current value)")
    m.add_argument("text")
    m.add_argument("--kind", default="fact", choices=["fact", "preference", "episode", "instruction"])
    m.add_argument("--importance", type=float, default=0.5)
    m.add_argument("--subject")
    m.add_argument("--attribute")
    m.add_argument("--valid-from", help="when the fact became true, e.g. 2026-04-12")
    m.add_argument("--replaces", nargs="*", help="memory ids this one supersedes")
    m.add_argument("--ttl", help="SurrealQL duration, e.g. 7d")
    for name, help in [("recall", "best memories for a query"), ("lookup", "a slot's value")]:
        m = msub.add_parser(name, help=help)
        if name == "recall":
            m.add_argument("text")
            m.add_argument("-k", type=int, default=5)
        else:
            m.add_argument("subject")
            m.add_argument("attribute")
        m.add_argument("--as-of", help="what was true at this time")
        m.add_argument("--known-at", help="what the agent believed at this time")
    m = msub.add_parser("history", help="every value a slot has had")
    m.add_argument("subject")
    m.add_argument("attribute")
    m = msub.add_parser("lineage", help="a memory and everything it superseded")
    m.add_argument("id")
    m = msub.add_parser("list", help="list memories")
    m.add_argument("--all", action="store_true", help="include superseded memories")
    msub.add_parser("erase", help="delete every memory and conversation turn for this user")

    add("users", cmd_users, "create the demo users")

    p = add("eval", cmd_eval, "evaluate retrievers on the golden set")
    p.add_argument("path", nargs="?", default=str(DATA / "corpus"))
    p.add_argument("--golden", default=str(DATA / "golden.json"))
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--strategies", default="section,fixed,sentence,parent_child")
    p.add_argument("--retrievers", default="vector,fulltext,hybrid,hybrid_graph")
    p.add_argument("--misses", action="store_true", help="list questions with recall below 1")

    add("reset", cmd_reset, "delete the database")

    args = parser.parse_args(argv)
    from contextlayer.db import EmbedderMismatch

    try:
        args.fn(args)
    except EmbedderMismatch as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
