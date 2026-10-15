import time

import pytest

from contextlayer.access import connect_as_user, create_user, ensure_viewer
from contextlayer.db import Database
from contextlayer.context import ContextBuilder
from contextlayer.db import QueryError
from contextlayer.memory import MemoryStore
from contextlayer.retrieval import Retriever, rank_documents


def test_remember_dedupes_and_reinforces(db, embedder):
    m = MemoryStore(db, embedder, agent="t")
    a = m.remember("The user owns the ledger service.", importance=0.4)
    b = m.remember("The user owns the ledger service.", importance=0.9)
    assert (a.action, b.action) == ("created", "reinforced")
    assert a.id == b.id
    [mem] = m.all()
    assert mem.importance == 0.9 and mem.access_count == 1


def test_recall_reinforces_what_it_returns(db, embedder):
    m = MemoryStore(db, embedder, agent="t")
    m.remember("The user prefers short answers.", kind="preference", importance=0.5)
    m.remember("The user is on the payments team.", importance=0.5)
    [top] = m.recall("short answers please", k=1)
    assert top.content == "The user prefers short answers."
    after = {x.content: x for x in m.all()}
    assert after["The user prefers short answers."].access_count == 1
    assert after["The user prefers short answers."].importance == pytest.approx(0.55)
    assert after["The user is on the payments team."].access_count == 0


def test_expired_memories_are_hidden_then_forgotten(db, embedder):
    m = MemoryStore(db, embedder, agent="t")
    m.remember("Permanent fact.")
    m.remember("Short-lived fact.", ttl="1s")
    time.sleep(1.2)
    assert [x.content for x in m.recall("fact", k=5)] == ["Permanent fact."]
    assert m.forget_expired() == 1
    assert [x.content for x in m.all()] == ["Permanent fact."]


def test_memories_are_scoped_to_agent(db, embedder):
    MemoryStore(db, embedder, agent="a").remember("Belongs to agent a.")
    assert MemoryStore(db, embedder, agent="b").recall("agent", k=5) == []


def test_graph_recall_through_about_edges(corpus_db, embedder):
    m = MemoryStore(corpus_db, embedder, agent="t")
    m.remember("The user is on call for the ledger this week.", about=["document:ledger-service"])
    assert [x.content for x in m.about(["document:ledger-service"])] == ["The user is on call for the ledger this week."]


def test_record_users_only_see_permitted_documents(corpus_db, embedder, settings):
    create_user(corpus_db, email="pat@example.com", name="Pat", password="pw-1", teams=["payments"])
    create_user(corpus_db, email="sam@example.com", name="Sam", password="pw-2", teams=["security"])
    pat = connect_as_user("pat@example.com", "pw-1", settings)
    sam = connect_as_user("sam@example.com", "pw-2", settings)

    question = "security findings register keyrotator two-person approval"
    assert "document:security-findings-2026" not in rank_documents(Retriever(pat, embedder).hybrid(question, 10))
    assert "document:security-findings-2026" in rank_documents(Retriever(sam, embedder).hybrid(question, 10))

    assert pat.one("SELECT VALUE id FROM ONLY document:`runbook-ledger-lag`") is not None
    assert sam.one("SELECT VALUE id FROM ONLY document:`runbook-ledger-lag`") is None
    assert pat.one("SELECT VALUE password FROM user") == [None]  # can't read hashes


def test_record_users_cannot_write(corpus_db, settings):
    create_user(corpus_db, email="pat@example.com", name="Pat", password="pw-1", teams=["payments"])
    pat = connect_as_user("pat@example.com", "pw-1", settings)
    pat.query("DELETE document:glossary; UPDATE document:`checkout-api` SET title = 'x';")
    assert corpus_db.one("SELECT VALUE title FROM ONLY document:`checkout-api`") == "Checkout API"
    assert corpus_db.one("SELECT VALUE id FROM ONLY document:glossary") is not None


def test_viewer_cannot_change_schema_or_data(corpus_db, settings):
    viewer = Database.connect_database_user(*ensure_viewer(corpus_db), settings=settings)
    assert viewer.one("SELECT count() FROM document GROUP ALL") == [{"count": 20}]
    with pytest.raises(QueryError):
        viewer.query("REMOVE TABLE chunk")
    viewer.query("DELETE document")
    assert corpus_db.one("SELECT count() FROM document GROUP ALL") == [{"count": 20}]


def test_context_packet_cites_sources_and_respects_budget(corpus_db, embedder):
    builder = ContextBuilder(Retriever(corpus_db, embedder), MemoryStore(corpus_db, embedder),
                             k=6, max_distance=None, token_budget=250)
    packet = builder.build("idempotency key retention TTL")
    assert packet.tokens <= 250
    assert [s.index for s in packet.sources] == list(range(1, len(packet.sources) + 1))
    rendered = packet.render()
    assert '<source index="1"' in rendered


def test_rendered_context_keeps_commands_intact(corpus_db, embedder):
    packet = ContextBuilder(Retriever(corpus_db, embedder), k=3, max_distance=None,
                            expand_graph=False).build("relayctl skip partition offset")
    rendered = packet.render()
    assert "relayctl skip --group <group> --partition <n> --offset <o>" in rendered
    assert "&lt;" not in rendered and "&#x27;" not in rendered


def test_recall_ignores_unrelated_memories(db, embedder):
    m = MemoryStore(db, embedder, agent="t", min_similarity=0.3)
    m.remember("The user prefers short answers.")
    assert m.recall("postgres failover backups", k=5) == []
    assert [x.content for x in m.recall("short answers", k=5)] == ["The user prefers short answers."]


def test_building_context_does_not_reinforce_memories(corpus_db, embedder):
    memory = MemoryStore(corpus_db, embedder, agent="t", min_similarity=0.0)
    memory.remember("The user is on call for the ledger.", importance=0.5)
    builder = ContextBuilder(Retriever(corpus_db, embedder), memory)
    for _ in range(3):
        assert builder.build("ledger on call").memories
    [mem] = memory.all()
    assert (mem.access_count, mem.importance) == (0, 0.5)


def test_restricted_users_still_get_k_results(corpus_db, embedder, settings):
    create_user(corpus_db, email="pat@example.com", name="Pat", password="pw-1", teams=["payments"])
    pat = connect_as_user("pat@example.com", "pw-1", settings)
    hits = Retriever(pat, embedder).vector("security findings register keyrotator emergency", 5)
    assert len(hits) == 5
    assert "document:security-findings-2026" not in rank_documents(hits)


def test_forget_user_erases_memories_and_turns(db, embedder):
    m = MemoryStore(db, embedder, agent="t")
    turn = m.log_turn("s1", "user", "I'm on the payments team.")
    m.remember("The user is on the payments team.", subject="user", attribute="team", source_turn=turn)
    m.remember("The user moved to platform.", subject="user", attribute="team")
    other = MemoryStore(db, embedder, agent="someone-else")
    other.remember("Unrelated.")
    assert m.forget_user() == 3  # two memories (one superseded) and one turn
    assert m.all(include_superseded=True) == []
    assert db.one("SELECT VALUE content FROM turn") == []
    assert [x.content for x in other.all()] == ["Unrelated."]


def test_memories_stay_within_their_share_of_the_budget(corpus_db, embedder):
    memory = MemoryStore(corpus_db, embedder, agent="t", min_similarity=0.0)
    memory.remember("ledger " + "detail " * 60, importance=0.9)  # ~81 tokens
    memory.remember("The user owns the ledger.", importance=0.5)
    builder = ContextBuilder(Retriever(corpus_db, embedder), memory, token_budget=200, memory_share=0.25,
                             max_distance=None)
    packet = builder.build("ledger")
    assert [m.content for m in packet.memories] == ["The user owns the ledger."]
    assert len(packet.dropped_memories) == 1
    assert packet.sources, "sources still get the rest of the budget"
    assert packet.tokens <= 200


def test_cited_understands_lists_and_ranges():
    from contextlayer.context import Answer, ContextPacket, Source

    sources = [Source(i, f"document:d{i}", f"D{i}", "text") for i in range(1, 6)]
    packet = ContextPacket("q", sources, [], 0)
    cited = lambda text: [s.index for s in Answer(text, packet).cited]
    assert cited("Yes [1, 3], and see [4-5].") == [1, 3, 4, 5]
    assert cited("Only [2].") == [2]
    assert cited("Nothing [12] here, and [x].") == []


def test_viewer_password_from_settings_is_stable(corpus_db, settings):
    import dataclasses

    corpus_db.settings = dataclasses.replace(settings, viewer_pass="a-long-shared-secret")
    first = ensure_viewer(corpus_db)
    second = ensure_viewer(corpus_db)  # another process doing the same
    assert first == second
    viewer = Database.connect_database_user(*first, settings=settings)
    assert viewer.one("RETURN 1") == 1
