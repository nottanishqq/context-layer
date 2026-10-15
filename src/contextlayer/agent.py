"""An agent on top of the context layer.

The model gets four tools and decides when to use them:

- search_knowledge: hybrid + graph retrieval over the document corpus
- query_database:   text-to-SurrealQL for counting, listing and following links
- recall_memory:    what it has learned about this user before
- remember:         write something worth keeping for next time

Each user turn also arrives with the most relevant memories already attached,
so the model doesn't need to ask for them on every message.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING, Any, Callable

from contextlayer.context import ContextBuilder
from contextlayer.memory import MemoryStore
from contextlayer.text2surql import TextToSurql

if TYPE_CHECKING:
    from contextlayer.llm import LLM

SYSTEM = """You are the engineering assistant for Lumen Pay, a payments company.

You answer from the company's documentation and database, which you reach through tools. \
Search before answering any question about Lumen Pay's systems, processes or incidents; \
your own background knowledge does not know this company. Cite the sources you used by \
title. If the tools don't turn up an answer, say what you looked for and that you didn't find it.

Use query_database for questions about counts, lists, metadata or how documents link together; \
use search_knowledge for questions answered by what documents say.

When the user tells you something durable about themselves (their team, their role, how they \
like answers, what they are working on), call remember. Don't store anything you found in \
the documents; those are already searchable.

Give remember a subject and attribute whenever the fact fills a slot that holds one value at a \
time, such as ("user", "team") or ("user", "role"). Reuse the same pair when the user corrects or \
updates it: the old value is then kept as history and stops being current. If the user says when \
the change happened, pass valid_from. To answer what was true at an earlier date, call \
recall_memory with as_of."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge",
        "description": "Search Lumen Pay's internal documentation (services, runbooks, policies, incident "
                       "reports). Returns the most relevant passages, including documents reached by "
                       "following links from the best matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for, in natural language."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_database",
        "description": "Answer a question about document metadata or structure by generating and running "
                       "a read-only database query. Good for counting, listing by kind or tag, dates, and "
                       "which documents link to which.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "recall_memory",
        "description": "Recall what you have previously learned about this user. By default returns what "
                       "is true now; with as_of, what was true at that date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "as_of": {"type": "string", "description": "ISO 8601 date or datetime, e.g. 2026-04-01."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "Store a durable fact about the user for future conversations. Facts given a "
                       "subject and attribute replace the current value of that slot; the old value is kept "
                       "as history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact, as a full sentence about the user."},
                "kind": {"type": "string", "enum": ["fact", "preference", "episode", "instruction"]},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "subject": {"type": "string", "description": "Who or what the fact is about, e.g. 'user'."},
                "attribute": {"type": "string", "description": "The slot, e.g. 'team', 'role', 'answer_style'."},
                "valid_from": {"type": "string", "description": "ISO 8601 date the fact became true, if stated."},
            },
            "required": ["content", "kind", "importance"],
        },
    },
]


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    output: str


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class ContextAgent:
    def __init__(
        self,
        llm: LLM,
        context: ContextBuilder,
        memory: MemoryStore,
        sql: TextToSurql | None = None,
        *,
        max_steps: int = 8,
        on_tool: Callable[[ToolCall], None] | None = None,
    ):
        self.llm = llm
        self.context = context
        self.memory = memory
        self.sql = sql
        self.max_steps = max_steps
        self.on_tool = on_tool
        self.messages: list[dict[str, Any]] = []
        self.session = uuid.uuid4().hex
        self._turn_id: str | None = None  # the user turn being answered: provenance for remember

    # -------------------------------------------------------------- tools

    def _search_knowledge(self, query: str) -> str:
        packet = self.context.build(query)
        if not packet.sources:
            return "No relevant documents found."
        return "\n\n".join(
            f'<document id="{s.document_id}" title="{escape(s.title)}"'
            + (f' hops="{s.hops}"' if s.hops else "")
            + f">\n{s.text}\n</document>"  # body unescaped: see ContextPacket.render
            for s in packet.sources
        )

    def _query_database(self, question: str) -> str:
        if not self.sql:
            return "Database queries are not enabled."
        result = self.sql.ask(question)
        if not result.ok:
            return "Could not produce a valid query. Errors: " + "; ".join(e for _, e in result.attempts)
        return json.dumps({"query": result.query, "rows": result.rows}, default=str)[:8000]

    def _recall_memory(self, query: str, as_of: str | None = None) -> str:
        memories = self.memory.recall(query, as_of=as_of)
        lines = []
        for m in memories:
            span = f"from {m.valid_from:%Y-%m-%d}" + (f" until {m.valid_until:%Y-%m-%d}" if m.valid_until else "")
            lines.append(f"- ({m.kind}, {span}) {m.content}")
        return "\n".join(lines) or "Nothing remembered."

    def _remember(self, content: str, kind: str, importance: float, subject: str | None = None,
                  attribute: str | None = None, valid_from: str | None = None) -> str:
        if (subject is None) != (attribute is None):
            subject = attribute = None  # a half-specified slot is no slot
        result = self.memory.remember(
            content, kind=kind, importance=importance, subject=subject, attribute=attribute,
            valid_from=valid_from, source="conversation", source_turn=self._turn_id,
        )
        if result.action == "superseded":
            return f"Stored as {result.id}; it supersedes {', '.join(result.superseded)}, kept as history."
        return f"{result.action.capitalize()}: {result.id}."

    def run_tool(self, name: str, args: dict[str, Any]) -> str:
        handlers = {
            "search_knowledge": self._search_knowledge,
            "query_database": self._query_database,
            "recall_memory": self._recall_memory,
            "remember": self._remember,
        }
        if name not in handlers:
            raise ValueError(f"Unknown tool {name}")
        return handlers[name](**args)

    # --------------------------------------------------------------- loop

    def _with_memories(self, message: str) -> str:
        memories = self.memory.recall(message, k=3, reinforce=False)
        if not memories:
            return message
        lines = "\n".join(f"- ({m.kind}) {m.content}" for m in memories)
        return f"<memories_about_user>\n{lines}\n</memories_about_user>\n\n{message}"

    def send(self, message: str) -> Turn:
        """One user turn: loop until the model stops calling tools."""
        self._turn_id = self.memory.log_turn(self.session, "user", message)
        self.messages.append({"role": "user", "content": self._with_memories(message)})
        calls: list[ToolCall] = []
        for _ in range(self.max_steps):
            response = self.llm.create(system=SYSTEM, messages=self.messages, tools=TOOLS)
            self.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                self.memory.log_turn(self.session, "assistant", text)
                return Turn(text, calls)

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    output, is_error = self.run_tool(block.name, block.input), False
                except Exception as e:  # report tool failures to the model rather than crash
                    output, is_error = f"{type(e).__name__}: {e}", True
                call = ToolCall(block.name, block.input, output)
                calls.append(call)
                if self.on_tool:
                    self.on_tool(call)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
        return Turn("I stopped after reaching the step limit for this question.", calls)
