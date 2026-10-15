"""Corrections and history: facts are superseded, never overwritten."""

import time
from datetime import datetime, timezone

import pytest

from contextlayer.memory import MemoryStore

MARCH, APRIL_1, APRIL_12 = "2026-03-14T09:12:00Z", "2026-04-01T00:00:00Z", "2026-04-12T16:40:00Z"


@pytest.fixture
def store(db, embedder):
    return MemoryStore(db, embedder, agent="t", min_similarity=0.0)  # hash vectors score low


def test_slot_correction_supersedes_and_keeps_history(store, db):
    old = store.remember("Tobie works at Acme as CTO.", subject="tobie", attribute="employer", valid_from=MARCH)
    new = store.remember("Tobie works at SurrealDB.", subject="tobie", attribute="employer", valid_from=APRIL_12)
    assert new.action == "superseded" and new.superseded == [old.id]

    assert store.lookup("tobie", "employer").content == "Tobie works at SurrealDB."
    assert [m.content for m in store.recall("where does tobie work", k=5)] == ["Tobie works at SurrealDB."]

    history = store.history("tobie", "employer")
    assert [m.content for m in history] == ["Tobie works at Acme as CTO.", "Tobie works at SurrealDB."]
    assert history[0].superseded and not history[1].superseded
    assert history[0].valid_until == history[1].valid_from
    # The edge points from the correction to what it corrected.
    assert db.one("SELECT VALUE ->supersedes->memory FROM ONLY $id", {"id": db.one(f"RETURN {new.id}")}) == [
        db.one(f"RETURN {old.id}")
    ]


def test_as_of_answers_with_what_was_true_then(store):
    store.remember("Tobie works at Acme as CTO.", subject="tobie", attribute="employer", valid_from=MARCH)
    store.remember("Tobie works at SurrealDB.", subject="tobie", attribute="employer", valid_from=APRIL_12)
    assert store.lookup("tobie", "employer", as_of=APRIL_1).content == "Tobie works at Acme as CTO."
    assert store.lookup("tobie", "employer", as_of="2026-01-01T00:00:00Z") is None
    assert [m.content for m in store.recall("tobie employer", as_of=APRIL_1)] == ["Tobie works at Acme as CTO."]


def test_known_at_answers_with_what_we_believed_then(store, db):
    store.remember("Tobie works at Acme as CTO.", subject="tobie", attribute="employer", valid_from=MARCH)
    before_correction = db.one("RETURN time::now()")
    time.sleep(0.02)
    store.remember("Tobie works at SurrealDB.", subject="tobie", attribute="employer", valid_from=APRIL_12)
    # Before the correction arrived, we believed Acme was still current...
    assert store.lookup("tobie", "employer", known_at=before_correction).content == "Tobie works at Acme as CTO."
    # ...and now we know that, today, it isn't.
    assert store.lookup("tobie", "employer").content == "Tobie works at SurrealDB."


def test_a_correction_dated_before_the_fact_retracts_it(store):
    store.remember("The user is on the payments team.", subject="user", attribute="team", valid_from="2026-05-01")
    store.remember("The user has been on the platform team all year.", subject="user", attribute="team",
                   valid_from="2026-01-01")
    old, new = sorted(store.history("user", "team"), key=lambda m: m.created_at)
    assert old.valid_until == old.valid_from, "the old fact's valid interval is empty"
    assert store.lookup("user", "team", as_of="2026-06-01").content.startswith("The user has been on the platform")


def test_repeating_the_current_value_reinforces(store):
    first = store.remember("The user is on the payments team.", subject="user", attribute="team")
    again = store.remember("The user is on the payments team.", subject="user", attribute="team")
    assert again.action == "reinforced" and again.id == first.id
    assert len(store.history("user", "team")) == 1


def test_explicit_replaces(store):
    old = store.remember("The deploy freeze ends Friday.")
    new = store.remember("The deploy freeze was extended to Monday.", replaces=[old.id])
    assert new.superseded == [old.id]
    assert [m.content for m in store.lineage(new.id)] == [
        "The deploy freeze was extended to Monday.", "The deploy freeze ends Friday.",
    ]
    assert [m.content for m in store.all()] == ["The deploy freeze was extended to Monday."]
    assert len(store.all(include_superseded=True)) == 2


def test_reconciler_judges_close_neighbours_without_a_slot(db, embedder):
    calls = []

    def judge(existing, new):
        calls.append((existing, new))
        return "supersede" if "SurrealDB" in new else "keep_both"

    store = MemoryStore(db, embedder, agent="t", reconciler=judge, conflict_distance=0.9)
    store.remember("Tobie works at Acme as CTO.")
    result = store.remember("Tobie now works at SurrealDB.")
    assert result.action == "superseded"
    assert calls == [("Tobie works at Acme as CTO.", "Tobie now works at SurrealDB.")]
    assert [m.content for m in store.all()] == ["Tobie now works at SurrealDB."]


def test_reconciler_duplicate_reinforces_and_far_memories_are_not_judged(db, embedder):
    calls = []

    def judge(existing, new):
        calls.append(new)
        return "duplicate"

    store = MemoryStore(db, embedder, agent="t", reconciler=judge, conflict_distance=0.9)
    first = store.remember("The user likes terse answers.")
    assert store.remember("User prefers terse answers.").id == first.id
    store.conflict_distance = 0.0
    assert store.remember("Completely unrelated fact about payouts.").action == "created"
    assert calls == ["User prefers terse answers."]


def test_provenance_points_at_the_turn(store, db):
    turn = store.log_turn("session-1", "user", "I'm on the payments team now.")
    store.remember("The user is on the payments team.", subject="user", attribute="team",
                   source_turn=turn, confidence=0.8)
    m = store.lookup("user", "team")
    assert m.source_turn == turn and m.confidence == 0.8
    assert db.one(f"SELECT VALUE content FROM ONLY {turn}") == "I'm on the payments team now."


def test_slot_requires_both_parts(store):
    with pytest.raises(ValueError):
        store.remember("x", subject="user")


def test_a_future_dated_change_is_not_current_yet(store):
    store.remember("Pat is on the payments team.", subject="user", attribute="team", valid_from="2026-01-01")
    store.remember("Pat moves to the platform team.", subject="user", attribute="team", valid_from="2099-01-01")
    # lookup, all() and about() agree on what is true today...
    assert store.lookup("user", "team").content == "Pat is on the payments team."
    assert [m.content for m in store.all()] == ["Pat is on the payments team."]
    # ...and the new fact takes over on its date.
    assert store.lookup("user", "team", as_of="2099-06-01").content == "Pat moves to the platform team."
    old, new = store.history("user", "team")
    assert old.superseded and not new.superseded
    assert old.valid_at(datetime.now(timezone.utc)) and not new.valid_at(datetime.now(timezone.utc))
