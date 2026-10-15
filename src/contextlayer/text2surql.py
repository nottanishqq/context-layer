"""Text-to-SurrealQL: let the model answer questions about structure, not just text.

"How many runbooks do we have?" and "what links to the ledger service?" are
not similarity questions; they are queries. This module:

1. builds the prompt's schema section from the database's own DEFINE
   statements (INFO FOR DB / INFO FOR TABLE), so it cannot drift from reality;
2. picks few-shot examples by similarity to the question;
3. checks the generated query statically (one SELECT, no write keywords);
4. runs it as a read-only VIEWER user, with a timeout, so a query that slips
   past the static check still cannot change anything;
5. feeds parse/runtime errors back to the model for a bounded number of retries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from contextlayer.db import Database, QueryError
from contextlayer.embeddings import Embedder

if TYPE_CHECKING:
    from contextlayer.llm import LLM

# Tables the model should not query: internal bookkeeping and private data.
DEFAULT_EXCLUDE = {"surql_example", "memory", "about", "supersedes", "turn", "user"}

WRITE_KEYWORDS = {
    "CREATE", "UPDATE", "UPSERT", "DELETE", "INSERT", "RELATE", "DEFINE", "REMOVE",
    "ALTER", "REBUILD", "KILL", "LIVE", "USE", "BEGIN", "COMMIT", "CANCEL", "LET",
    "INFO", "SLEEP", "ACCESS", "SHOW", "THROW", "OPTION", "EXPLAIN", "PARALLEL",
}
FORBIDDEN_FUNCTIONS = ("http::", "crypto::", "sleep(")

_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`|⟨[^⟩]*⟩")
_COMMENT = re.compile(r"--[^\n]*|//[^\n]*|/\*.*?\*/", re.DOTALL)


class GuardrailError(ValueError):
    pass


def check_query(query: str, exclude: set[str] = DEFAULT_EXCLUDE) -> str:
    """Static checks. Returns the query, stripped, or raises GuardrailError.

    Leaving a table out of the prompt does not stop the model from querying it,
    and the VIEWER login can read every table, so excluded tables are refused here.
    """
    query = query.strip().rstrip(";").strip()
    # Scan with strings and comments blanked out, so 'DELETE' inside a literal is fine.
    code = _COMMENT.sub(" ", _STRING.sub("''", query))
    if ";" in code:
        raise GuardrailError("Only a single statement is allowed.")
    if not re.match(r"(?i)^\s*SELECT\b", code):
        raise GuardrailError("The query must be a SELECT statement.")
    words = {w.upper() for w in re.findall(r"[A-Za-z_]+", code)}
    if bad := sorted(words & WRITE_KEYWORDS):
        raise GuardrailError(f"Keyword not allowed: {', '.join(bad)}.")
    if bad := sorted(words & {t.upper() for t in exclude}):
        raise GuardrailError(f"Table not available: {', '.join(bad).lower()}.")
    if bad := [f for f in FORBIDDEN_FUNCTIONS if f in code.lower()]:
        raise GuardrailError(f"Function not allowed: {', '.join(bad)}.")
    return query


def schema_prompt(db: Database, exclude: set[str] = DEFAULT_EXCLUDE) -> str:
    """The schema as DEFINE statements, minus permissions, embeddings and excluded tables."""

    def clean(statement: str) -> str:
        return re.sub(r"\s+PERMISSIONS\b.*$", "", statement, flags=re.DOTALL)

    info = db.one("INFO FOR DB")
    lines = [clean(a) for a in info.get("analyzers", {}).values()]
    for table, define in sorted(info["tables"].items()):
        if table in exclude:
            continue
        lines.append("")
        lines.append(clean(define))
        table_info = db.one(f"INFO FOR TABLE {table}")
        for name, field_def in sorted(table_info["fields"].items()):
            if "embedding" in name or name == "password" or name.endswith(".*"):
                continue
            lines.append(clean(field_def))
        for index in table_info["indexes"].values():
            if "HNSW" not in index:
                lines.append(clean(index))
    return "\n".join(lines)


SYSTEM = """You translate questions into SurrealQL for SurrealDB 3.x.

Write exactly one read-only SELECT statement. Rules:
- Use only the tables and fields in the schema below.
- Record ids look like document:`ledger-service`; quote keys containing dashes with backticks.
- Follow record links with dots (chunk.document.title) and graph edges with arrows
  (document:`x`->links_to->document, document:`x`<-links_to<-document).
- For words inside document text, query the chunk table with strategy = 'section' and the
  full-text operator: text @@ 'term'.
- Never select embedding fields.

<schema>
{schema}
</schema>"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "A single SurrealQL SELECT statement."},
        "explanation": {"type": "string", "description": "One sentence on how the query answers the question."},
    },
    "required": ["query", "explanation"],
    "additionalProperties": False,
}


@dataclass
class SurqlResult:
    question: str
    query: str | None
    rows: Any
    explanation: str = ""
    attempts: list[tuple[str, str]] = field(default_factory=list)  # (query, error) for failed tries

    @property
    def ok(self) -> bool:
        return self.query is not None


class TextToSurql:
    def __init__(
        self,
        db: Database,
        llm: LLM | None,
        embedder: Embedder,
        *,
        executor: Database | None = None,
        max_rows: int = 50,
        timeout: str = "2s",
        exclude: set[str] = DEFAULT_EXCLUDE,
    ):
        """`db` introspects the schema and stores examples; `executor` runs generated
        queries and should be a read-only connection (see access.ensure_viewer).
        `llm` is only needed by ask()."""
        self.db = db
        self.llm = llm
        self.embedder = embedder
        self.executor = executor or db
        self.max_rows = max_rows
        self.timeout = timeout
        self.exclude = exclude
        self._schema: str | None = None

    @property
    def schema(self) -> str:
        if self._schema is None:
            self._schema = schema_prompt(self.db, self.exclude)
        return self._schema

    def load_examples(self, path: str | Path) -> int:
        examples = json.loads(Path(path).read_text(encoding="utf-8"))
        vectors = self.embedder.embed([e["question"] for e in examples])
        self.db.query(
            "DELETE surql_example; INSERT INTO surql_example $rows;",
            {"rows": [{**e, "embedding": v} for e, v in zip(examples, vectors)]},
        )
        return len(examples)

    def examples_for(self, question: str, k: int = 3) -> list[dict[str, str]]:
        return self.db.one(
            f"SELECT question, query, vector::distance::knn() AS d FROM surql_example "
            f"WHERE embedding <|{int(k)},64|> $qv ORDER BY d",
            {"qv": self.embedder.embed_query(question)},
        )

    def build_prompt(self, question: str, feedback: list[tuple[str, str]]) -> str:
        parts = []
        if examples := self.examples_for(question):
            shots = "\n\n".join(f"Question: {e['question']}\nQuery: {e['query']}" for e in examples)
            parts.append(f"<examples>\n{shots}\n</examples>")
        for query, error in feedback:
            parts.append(f"<failed_attempt>\nQuery: {query}\nError: {error}\n</failed_attempt>")
        if feedback:
            parts.append("Write a corrected query.")
        parts.append(f"Question: {question}")
        return "\n\n".join(parts)

    def run(self, query: str) -> Any:
        """Check and execute a query with the guardrails applied."""
        query = check_query(query, self.exclude)
        if not re.search(r"(?i)\bTIMEOUT\b", _STRING.sub("''", query)):
            query = f"{query} TIMEOUT {self.timeout}"
        rows = self.executor.one(query)
        return rows[: self.max_rows] if isinstance(rows, list) else rows

    def ask(self, question: str, max_attempts: int = 3) -> SurqlResult:
        failures: list[tuple[str, str]] = []
        system = SYSTEM.format(schema=self.schema)
        for _ in range(max_attempts):
            out = self.llm.json(system=system, prompt=self.build_prompt(question, failures), schema=OUTPUT_SCHEMA)
            try:
                rows = self.run(out["query"])
                return SurqlResult(question, out["query"], rows, out.get("explanation", ""), failures)
            except (GuardrailError, QueryError) as e:
                failures.append((out["query"], str(e)))
        return SurqlResult(question, None, None, "", failures)
