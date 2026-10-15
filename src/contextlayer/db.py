"""A thin wrapper around the SurrealDB Python SDK.

The SDK's `query()` only returns the result of the *first* statement, and the
context layer runs a lot of multi-statement SurrealQL (LET ... ; SELECT ...).
`Database.query()` returns every statement's result and raises on the first
error, so callers never have to inspect raw RPC envelopes.
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING, Any

from surrealdb import RecordID, Surreal

from contextlayer.config import Settings

if TYPE_CHECKING:
    from contextlayer.embeddings import Embedder


class QueryError(RuntimeError):
    """A SurrealQL statement failed."""

    def __init__(self, message: str, statement: int | None = None):
        where = f" (statement {statement})" if statement is not None else ""
        super().__init__(f"SurrealQL error{where}: {message}")
        self.statement = statement


class EmbedderMismatch(RuntimeError):
    """The embedder in use is not the one that filled the database."""


class Database:
    def __init__(self, conn: Any, settings: Settings):
        self.conn = conn
        self.settings = settings

    @classmethod
    def connect(cls, settings: Settings | None = None, *, signin: bool = True) -> "Database":
        """Open a connection as the root user from settings and select the namespace/database.

        With `signin=False` the connection stays anonymous, which is how a
        record user connects before calling `signin_record()`.
        """
        settings = settings or Settings()
        conn = Surreal(settings.surreal_url)
        if signin:
            conn.signin({"username": settings.surreal_user, "password": settings.surreal_pass})
        conn.use(settings.surreal_ns, settings.surreal_db)
        return cls(conn, settings)

    @classmethod
    def connect_database_user(cls, username: str, password: str, settings: Settings | None = None) -> "Database":
        """Connect as a system user defined ON DATABASE (e.g. one with the VIEWER role)."""
        settings = settings or Settings()
        conn = Surreal(settings.surreal_url)
        conn.signin({"namespace": settings.surreal_ns, "database": settings.surreal_db,
                     "username": username, "password": password})
        conn.use(settings.surreal_ns, settings.surreal_db)
        return cls(conn, settings)

    def signin_record(self, access: str, **credentials: Any) -> str:
        """Sign in as a record user through a `DEFINE ACCESS ... TYPE RECORD` method."""
        return self.conn.signin({
            "namespace": self.settings.surreal_ns,
            "database": self.settings.surreal_db,
            "access": access,
            "variables": credentials,
        })

    def query(self, sql: str, vars: dict[str, Any] | None = None) -> list[Any]:
        """Run SurrealQL and return the result of every statement, in order."""
        response = self.conn.query_raw(sql, vars or {})
        if "error" in response:
            raise QueryError(response["error"].get("message", str(response["error"])))
        statements = response["result"]
        failed = [(i, s) for i, s in enumerate(statements) if s["status"] != "OK"]
        if failed:
            # Inside a transaction every statement fails; report the one that caused it.
            cause = [(i, s) for i, s in failed if not str(s["result"]).startswith("The query was not executed")]
            i, stmt = (cause or failed)[0]
            raise QueryError(str(stmt["result"]), statement=i)
        return [s["result"] for s in statements]

    def one(self, sql: str, vars: dict[str, Any] | None = None) -> Any:
        """Run SurrealQL and return the result of the last statement."""
        return self.query(sql, vars)[-1]

    def apply_schema(self, embedder: Embedder) -> None:
        """Apply the bundled schema (every statement is idempotent) and record the embedder."""
        sql = resources.files("contextlayer").joinpath("schema.surql").read_text(encoding="utf-8")
        self.query(sql.replace("{{EMBEDDING_DIM}}", str(embedder.dim)))
        self.check_embedder(embedder, record=True)

    def check_embedder(self, embedder: Embedder, *, record: bool = False) -> None:
        """Refuse to mix vectors from two models in one database.

        The vector index is created once with a fixed dimension, and a model of
        the same dimension would mix in silently, turning search into noise.
        """
        try:
            stored = self.one("SELECT model, dim FROM ONLY meta:embedding")
        except QueryError:
            stored = None  # the schema hasn't been applied yet
        if stored is None:
            if not record:
                raise EmbedderMismatch("This database has no embedding model recorded. Run `ctx init` first.")
            self.query("UPSERT meta:embedding CONTENT { model: $model, dim: $dim }",
                       {"model": embedder.name, "dim": embedder.dim})
        elif (stored["model"], stored["dim"]) != (embedder.name, embedder.dim):
            raise EmbedderMismatch(
                f"This database was embedded with {stored['model']} ({stored['dim']} dimensions), "
                f"not {embedder.name} ({embedder.dim}). Use the same model, or `ctx reset` and re-ingest."
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def rid(table: str, key: str) -> RecordID:
    """Build a record id, e.g. rid("document", "auth-service")."""
    return RecordID(table, key)


def rid_str(value: Any) -> str:
    """Render a RecordID as `table:key` (pass-through for anything else)."""
    if isinstance(value, RecordID):
        return f"{value.table_name}:{value.id}"
    return str(value)


def parse_rid(value: str) -> RecordID:
    """Parse `table:key` back into a RecordID."""
    table, key = value.split(":", 1)
    return RecordID(table, key)
