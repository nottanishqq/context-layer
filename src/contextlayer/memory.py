"""Agent memory: what an agent has learned, and how it gets it back.

Recall is not plain similarity search. A memory's score blends three things:

    score = w_sim * similarity + w_rec * recency + w_imp * importance

- similarity: cosine similarity between the query and the memory.
- recency:    0.5 ** (hours since last accessed / half_life_hours).
- importance: 0..1, set when the memory is written and nudged up every time
              it is recalled, so memories that keep proving useful rise.

Corrections never overwrite. When a new memory contradicts a current one,
the old memory's valid time and record time are closed, a `supersedes` edge
links new to old, and the old row stays. Recall returns only what is current
unless you ask about another time:

- `as_of`:    what was true at that moment (valid time).
- `known_at`: what the agent believed at that moment (record time).

A memory contradicts another in one of two ways:

- Slots. A memory written with a (subject, attribute) slot, such as
  ("user", "team"), supersedes whatever currently fills that slot. Cheap,
  deterministic, and the writer has to know the slot.
- A reconciler. For memories without a slot, a judge (usually an LLM call)
  decides whether a close neighbour is superseded, duplicated, or unrelated.

Memories also expire (`expires_at`), carry provenance (`source_turn`,
`confidence`), and link to the documents they're about (`->about->`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal

from contextlayer.db import Database, parse_rid, rid_str
from contextlayer.embeddings import Embedder

if TYPE_CHECKING:
    from contextlayer.llm import LLM

Verdict = Literal["supersede", "duplicate", "keep_both"]
# (existing memory, new memory) -> how the new one relates to the existing one
Reconciler = Callable[[str, str], Verdict]


@dataclass
class Memory:
    id: str
    content: str
    kind: str
    importance: float
    created_at: datetime
    last_accessed: datetime
    access_count: int
    valid_from: datetime
    valid_until: datetime | None = None
    superseded_at: datetime | None = None
    subject: str | None = None
    attribute: str | None = None
    confidence: float = 1.0
    source_turn: str | None = None
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def superseded(self) -> bool:
        """A newer memory replaced this one (it may still be true until its valid_until)."""
        return self.superseded_at is not None

    def valid_at(self, moment: datetime) -> bool:
        """True at `moment`, as the agent knows things now: the same rule recall uses."""
        return self.valid_from <= moment and (self.valid_until is None or self.valid_until > moment)


@dataclass
class WriteResult:
    id: str
    action: Literal["created", "reinforced", "superseded"]
    superseded: list[str] = field(default_factory=list)


def _memory(row: dict[str, Any]) -> Memory:
    return Memory(
        id=rid_str(row["id"]),
        content=row["content"],
        kind=row["kind"],
        importance=row["importance"],
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
        access_count=row["access_count"],
        valid_from=row["valid_from"],
        valid_until=row.get("valid_until"),
        superseded_at=row.get("superseded_at"),
        subject=row.get("subject"),
        attribute=row.get("attribute"),
        confidence=row.get("confidence", 1.0),
        source_turn=rid_str(row["source_turn"]) if row.get("source_turn") else None,
        score=row.get("score", 0.0),
        signals={k: row[k] for k in ("similarity", "recency") if k in row},
    )


# Statements that set $t_valid and $t_known (default: now), and the filter
# that keeps memories true at $t_valid as the agent knew things at $t_known.
#
# A superseded memory is not disbelieved: after "Tobie moved to SurrealDB on
# 12 April" we still believe he was at Acme *until* 12 April. What changed at
# `superseded_at` is the memory's end date. So, as known at $t_known, a memory
# ends at `valid_until` if that end had been recorded by then, and is
# open-ended otherwise.
_AT_TIME = """
    LET $t_valid = IF $as_of THEN <datetime>$as_of ELSE time::now() END;
    LET $t_known = IF $known_at THEN <datetime>$known_at ELSE time::now() END;
"""
_IN_TIME = """
    AND created_at <= $t_known
    AND valid_from <= $t_valid
    AND (valid_until IS NONE OR superseded_at > $t_known OR valid_until > $t_valid)
    AND (expires_at IS NONE OR expires_at > time::now())
"""


class MemoryStore:
    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        agent: str = "default",
        *,
        owner: str | None = None,
        half_life_hours: float = 72.0,
        weights: tuple[float, float, float] = (0.6, 0.2, 0.2),
        dedupe_distance: float = 0.05,
        reinforce_step: float = 0.05,
        reconciler: Reconciler | None = None,
        conflict_distance: float = 0.35,
        min_similarity: float = 0.5,
    ):
        """`min_similarity` keeps unrelated memories out of recall. 0.5 suits
        bge-small, where unrelated sentences score around 0.4; other models need
        their own value, and the offline HashEmbedder scores far lower."""
        db.check_embedder(embedder)
        self.db = db
        self.embedder = embedder
        self.agent = agent
        self.owner = parse_rid(owner) if owner else None
        self.half_life_hours = half_life_hours
        self.weights = weights
        self.dedupe_distance = dedupe_distance
        self.reinforce_step = reinforce_step
        self.reconciler = reconciler
        self.conflict_distance = conflict_distance
        self.min_similarity = min_similarity

    @property
    def _scope(self) -> str:
        return "agent = $agent AND owner = $owner"

    def _vars(self, **extra: Any) -> dict[str, Any]:
        return {"agent": self.agent, "owner": self.owner, **extra}

    # ------------------------------------------------------------------ write

    def remember(
        self,
        content: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        subject: str | None = None,
        attribute: str | None = None,
        valid_from: datetime | str | None = None,
        replaces: list[str] | None = None,
        confidence: float = 1.0,
        source: str | None = None,
        source_turn: str | None = None,
        ttl: str | None = None,
        about: list[str] | None = None,
    ) -> WriteResult:
        """Store a memory, reconciling it against what is already known.

        - A near-identical current memory is reinforced instead of duplicated.
        - With a (subject, attribute) slot, current memories in that slot are superseded.
        - Without one, the reconciler (if any) judges close neighbours.
        - `replaces` names memories to supersede explicitly.

        `valid_from` is when the fact became true (default: now); a correction
        dated before the fact it replaces marks the old fact as never having
        been true. `ttl` is a SurrealQL duration such as "7d".
        """
        if (subject is None) != (attribute is None):
            raise ValueError("subject and attribute go together")
        embedding = self.embedder.embed([content])[0]
        # shortcut: read-then-write. Two processes correcting the same slot at once can
        # both supersede the old value and leave two current ones. Fine for one user at a
        # time; for many, read the slot inside the transaction below.
        neighbours = self._current_neighbours(embedding, subject, attribute)

        if neighbours and neighbours[0]["distance"] <= self.dedupe_distance:
            return self._reinforce(neighbours[0]["id"], importance)

        supersede = [parse_rid(r) for r in replaces or []]
        if subject is not None:
            supersede += [n["id"] for n in neighbours]
        elif self.reconciler:
            for n in neighbours:
                if n["distance"] > self.conflict_distance:
                    break
                verdict = self.reconciler(n["content"], content)
                if verdict == "duplicate":
                    return self._reinforce(n["id"], importance)
                if verdict == "supersede":
                    supersede.append(n["id"])

        new_id = self.db.query(
            """
            BEGIN TRANSACTION;
            LET $from = IF $valid_from THEN <datetime>$valid_from ELSE time::now() END;
            LET $new = (CREATE ONLY memory CONTENT {
                agent: $agent, owner: $owner, kind: $kind, content: $content,
                subject: $subject, attribute: $attribute,
                importance: $importance, embedding: $embedding,
                valid_from: $from, confidence: $confidence,
                source: $source, source_turn: $source_turn,
                expires_at: IF $ttl THEN time::now() + <duration>$ttl END,
            }).id;
            FOR $old IN $supersede {
                -- Close the old fact's valid time where the new one starts (or at its own
                -- start, if the correction says it was never true) and its record time now.
                UPDATE $old SET
                    valid_until = IF $from > valid_from THEN $from ELSE valid_from END,
                    superseded_at = time::now();
                RELATE $new->supersedes->$old;
            };
            COMMIT TRANSACTION;
            RETURN $new;
            """,
            self._vars(
                content=content, kind=kind, subject=subject, attribute=attribute, importance=importance,
                embedding=embedding, valid_from=valid_from, confidence=confidence, source=source,
                source_turn=parse_rid(source_turn) if source_turn else None, ttl=ttl,
                supersede=supersede,
            ),
        )[-1]
        if about:
            self.db.query(
                "INSERT RELATION IGNORE INTO about $edges",
                {"edges": [{"in": new_id, "out": parse_rid(d)} for d in about]},
            )
        return WriteResult(rid_str(new_id), "superseded" if supersede else "created",
                           [rid_str(s) for s in supersede])

    def _current_neighbours(self, embedding: list[float], subject: str | None,
                            attribute: str | None) -> list[dict[str, Any]]:
        """The latest-recorded memories nearest to `embedding`: everything in the slot, or the closest 3.

        "Latest-recorded" (not yet superseded) rather than "true now": a new fact is
        reconciled against the newest thing we were told, even one dated in the future.
        """
        slot = "AND subject = $subject AND attribute = $attribute" if subject is not None else ""
        limit = 50 if subject is not None else 3
        return self.db.one(
            f"""
            SELECT id, content, 1 - vector::similarity::cosine(embedding, $embedding) AS distance
            FROM memory
            WHERE {self._scope} {slot}
                AND superseded_at IS NONE
                AND (expires_at IS NONE OR expires_at > time::now())
            ORDER BY distance LIMIT {limit}
            """,
            self._vars(embedding=embedding, subject=subject, attribute=attribute),
        )

    def _reinforce(self, memory_id: Any, importance: float) -> WriteResult:
        self.db.query(
            "UPDATE $id SET importance = math::max([importance, $importance]), "
            "access_count += 1, last_accessed = time::now()",
            {"id": memory_id, "importance": importance},
        )
        return WriteResult(rid_str(memory_id), "reinforced")

    # ------------------------------------------------------------------- read

    def recall(
        self,
        query: str,
        k: int = 5,
        *,
        kinds: list[str] | None = None,
        as_of: datetime | str | None = None,
        known_at: datetime | str | None = None,
        reinforce: bool = True,
    ) -> list[Memory]:
        """The k best memories for `query` by blended score.

        By default only current memories are considered. `as_of` asks what was
        true at another time; `known_at` asks what the agent believed then.
        Reinforcement (access count, importance, recency) applies only to
        recall of the present.

        This is an exact scan, not an HNSW search: the scope filter (one agent,
        one user) is selective and backed by an index, and one user's memories
        are few enough to compare against the query directly. Exact search also
        never misses, which approximate search over a filtered subset can.
        """
        w_sim, w_rec, w_imp = self.weights
        kind_filter = "AND kind IN $kinds" if kinds else ""
        reinforce = reinforce and as_of is None and known_at is None
        rows = self.db.query(
            _AT_TIME + f"""
            LET $candidates = SELECT *,
                    vector::similarity::cosine(embedding, $qv) AS similarity,
                    math::pow(0.5, duration::secs(time::now() - last_accessed) / 3600.0 / $half_life) AS recency
                FROM memory
                WHERE {self._scope} {kind_filter} {_IN_TIME};
            LET $ranked = SELECT *, ($w_sim * similarity + $w_rec * recency + $w_imp * importance) AS score
                OMIT embedding
                FROM $candidates
                -- Without a floor, recall always returns k memories, relevant or not.
                WHERE similarity >= $min_similarity
                ORDER BY score DESC LIMIT {int(k)};
            IF $reinforce {{
                UPDATE $ranked.id SET
                    access_count += 1,
                    last_accessed = time::now(),
                    importance = math::min([importance + $step, 1.0]);
            }};
            RETURN $ranked;
            """,
            self._vars(qv=self.embedder.embed_query(query), kinds=kinds, as_of=as_of, known_at=known_at,
                       half_life=self.half_life_hours, w_sim=w_sim, w_rec=w_rec, w_imp=w_imp,
                       reinforce=reinforce, step=self.reinforce_step, min_similarity=self.min_similarity),
        )[-1]
        return [_memory(r) for r in rows]

    def lookup(
        self,
        subject: str,
        attribute: str,
        *,
        as_of: datetime | str | None = None,
        known_at: datetime | str | None = None,
    ) -> Memory | None:
        """The value of a slot at a point in time: a direct lookup, no embedding needed."""
        rows = self.db.query(
            _AT_TIME + f"""
            SELECT * OMIT embedding FROM memory
            WHERE {self._scope} AND subject = $subject AND attribute = $attribute {_IN_TIME}
            ORDER BY valid_from DESC LIMIT 1;
            """,
            self._vars(subject=subject, attribute=attribute, as_of=as_of, known_at=known_at),
        )[-1]
        return _memory(rows[0]) if rows else None

    def history(self, subject: str, attribute: str) -> list[Memory]:
        """Every value a slot has had, current or superseded, oldest first."""
        rows = self.db.one(
            f"""
            SELECT * OMIT embedding FROM memory
            WHERE {self._scope} AND subject = $subject AND attribute = $attribute
            ORDER BY valid_from, created_at
            """,
            self._vars(subject=subject, attribute=attribute),
        )
        return [_memory(r) for r in rows]

    def lineage(self, memory_id: str) -> list[Memory]:
        """A memory followed by everything it superseded, transitively, newest first."""
        rows = self.db.one(
            """
            LET $older = $id.{..+collect}(->supersedes->memory);
            SELECT * OMIT embedding FROM array::concat([$id], $older)
            ORDER BY created_at DESC;
            """,
            {"id": parse_rid(memory_id)},
        )
        return [_memory(r) for r in rows]

    def about(self, document_ids: list[str]) -> list[Memory]:
        """Memories true now and linked to any of these documents (graph recall)."""
        rows = self.db.query(
            _AT_TIME + f"""
            SELECT * OMIT embedding FROM array::distinct(array::flatten($docs<-about<-memory))
            WHERE {self._scope} {_IN_TIME}
            ORDER BY importance DESC;
            """,
            self._vars(docs=[parse_rid(d) for d in document_ids], as_of=None, known_at=None),
        )[-1]
        return [_memory(r) for r in rows]

    def all(self, *, include_superseded: bool = False) -> list[Memory]:
        """Memories true now, or with `include_superseded`, every memory ever stored."""
        current = "" if include_superseded else _IN_TIME
        rows = self.db.query(
            _AT_TIME + f"SELECT * OMIT embedding FROM memory WHERE {self._scope} {current} ORDER BY created_at;",
            self._vars(as_of=None, known_at=None),
        )[-1]
        return [_memory(r) for r in rows]

    # ----------------------------------------------------------------- forget

    def forget(self, memory_id: str) -> None:
        """Delete a memory outright (and with it, its edges). For erasure, not correction."""
        self.db.query("DELETE $id", {"id": parse_rid(memory_id)})

    def forget_user(self) -> int:
        """Erase everything this agent holds about its owner: memories and the
        conversation turns they came from. Returns how many records were deleted."""
        deleted = self.db.query(
            f"""
            DELETE memory WHERE {self._scope} RETURN BEFORE;
            DELETE turn WHERE {self._scope} RETURN BEFORE;
            """,
            self._vars(),
        )
        return sum(len(d or []) for d in deleted)

    def forget_expired(self) -> int:
        """Delete every expired memory for this agent. Returns how many were deleted."""
        deleted = self.db.one(
            # `expires_at != NONE` matters: NONE sorts before every datetime, so
            # `NONE <= time::now()` is true and would delete every permanent memory.
            f"DELETE memory WHERE {self._scope} AND expires_at != NONE AND expires_at <= time::now() RETURN BEFORE",
            self._vars(),
        )
        return len(deleted or [])

    # ------------------------------------------------------------ provenance

    def log_turn(self, session: str, role: str, content: str) -> str:
        """Record a conversation turn; memories extracted from it can cite it as `source_turn`."""
        return rid_str(self.db.one(
            "RETURN (CREATE ONLY turn CONTENT { agent: $agent, owner: $owner, session: $session_id, "
            "role: $role, content: $content }).id",
            self._vars(session_id=session, role=role, content=content),
        ))


JUDGE_SYSTEM = """You maintain an assistant's long-term memory about a user.
Given an EXISTING memory and a NEW one, decide how the new one relates to the existing one:
- "supersede": they are about the same thing and the new one replaces it (a correction or a change).
- "duplicate": they say the same thing.
- "keep_both": they are about different things, or both can be true at once."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supersede", "duplicate", "keep_both"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


def llm_reconciler(llm: LLM) -> Reconciler:
    """A reconciler that asks the model. One call per close neighbour, so only near ones are judged."""

    def judge(existing: str, new: str) -> Verdict:
        prompt = f"<existing>{existing}</existing>\n<new>{new}</new>"
        return llm.json(system=JUDGE_SYSTEM, prompt=prompt, schema=JUDGE_SCHEMA)["verdict"]

    return judge
