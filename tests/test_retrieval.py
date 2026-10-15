from pathlib import Path

import pytest

from contextlayer.ingest import Ingestor, SourceDocument, parse_markdown
from contextlayer.retrieval import Retriever, rank_documents

from conftest import CORPUS


def test_parse_markdown_reads_front_matter_and_links():
    doc = parse_markdown(CORPUS / "incident-2026-03-duplicate-charges.md")
    assert doc.slug == "incident-2026-03-duplicate-charges"
    assert doc.kind == "incident"
    assert doc.metadata["date"] == "2026-03-14"
    assert [t for t, _ in doc.links] == ["checkout-api", "idempotency-keys"]
    assert "[[" not in doc.plain_text


def test_ingest_is_idempotent_and_removes_stale_chunks(db, embedder):
    ingestor = Ingestor(db, embedder, ["section"])
    long = SourceDocument("doc-a", "Doc A", "## One\n\nFirst.\n\n## Two\n\nSecond.\n\n## Three\n\nThird.")
    ingestor.ingest(long)
    ingestor.ingest(long)
    assert db.one("SELECT count() FROM chunk GROUP ALL") == [{"count": 3}]

    shorter = SourceDocument("doc-a", "Doc A", "## One\n\nFirst, revised.")
    ingestor.ingest(shorter)
    texts = db.one("SELECT VALUE text FROM chunk")
    assert texts == ["Doc A\nOne\n\nFirst, revised."]


def test_links_skip_missing_targets(db, embedder):
    ingestor = Ingestor(db, embedder)
    a = SourceDocument("a", "A", "See [[b]] and [[missing]].")
    b = SourceDocument("b", "B", "Plain.")
    stats = ingestor.ingest_all([a, b])
    assert stats["links"] == 1
    assert db.one("SELECT VALUE ->links_to->document FROM ONLY document:a") == [db.one("RETURN document:b")]


def test_fulltext_finds_exact_error_codes(corpus_db, embedder):
    hits = Retriever(corpus_db, embedder).fulltext("ERR_CHECKOUT_429", k=3)
    assert "document:rate-limiting" in rank_documents(hits)


def test_ann_matches_exact_search(corpus_db, embedder):
    r = Retriever(corpus_db, embedder)
    question = "How long are application logs kept?"
    ann = [h.chunk_id for h in r.vector(question, 5)]
    exact = [h.chunk_id for h in r.vector(question, 5, exact=True)]
    assert ann == exact


def test_kind_filter(corpus_db, embedder):
    hits = Retriever(corpus_db, embedder).vector("what to do when paged", 5, kinds=["runbook"])
    assert hits and all(h.kind == "runbook" for h in hits)


def test_builtin_and_handwritten_rrf_agree(corpus_db, embedder):
    r = Retriever(corpus_db, embedder)
    question = "why were customers charged twice"
    builtin = [(h.chunk_id, round(h.score, 9)) for h in r.hybrid(question, 5)]
    python = [(h.chunk_id, round(h.score, 9)) for h in r.hybrid(question, 5, fusion="python")]
    assert builtin == python


def test_expand_follows_links_and_records_the_path(corpus_db, embedder):
    r = Retriever(corpus_db, embedder)
    seed = r.fulltext("notification service emails", k=1)
    assert seed[0].document_id == "document:notification-service"
    expanded = r.expand("consumer lag emails late", seed, depth=2, per_hop=1)
    path = [(h.document_id, h.signals["hops"], h.signals["via"]) for h in expanded]
    assert ("document:queue-platform", 1, "document:notification-service") in path
    assert any(hops == 2 and via == "document:queue-platform" for _, hops, via in path)


def test_graph_helpers(corpus_db, embedder):
    r = Retriever(corpus_db, embedder)
    assert "document:runbook-consumer-lag" in r.neighbourhood("document:notification-service", 2)
    path = r.shortest_path("document:incident-2026-03-duplicate-charges", "document:postgres-cluster")
    assert path[0] == "document:incident-2026-03-duplicate-charges"
    assert path[-1] == "document:postgres-cluster"


def test_prune_removes_documents_whose_files_are_gone(db, embedder):
    ingestor = Ingestor(db, embedder)
    a, b = SourceDocument("a", "A", "Alpha."), SourceDocument("b", "B", "Beta.")
    ingestor.ingest_all([a, b])
    stats = ingestor.ingest_all([a], prune=True)
    assert stats["removed"] == 1
    assert db.one("SELECT VALUE id FROM document") == [db.one("RETURN document:a")]
    assert db.one("SELECT VALUE text FROM chunk") == ["A\nAlpha."]


def test_duplicate_slugs_are_refused(tmp_path):
    from contextlayer.ingest import load_directory

    for folder in ("runbooks", "services"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "ledger.md").write_text("Text.", encoding="utf-8")
    with pytest.raises(ValueError, match="both have the slug 'ledger'"):
        load_directory(tmp_path)


def test_parent_child_retrieves_small_and_returns_the_parent(db, embedder):
    doc = SourceDocument("ops", "Ops", "## Failover\n\nPatroni promotes a replica. PgBouncer follows it. "
                                       "Writes fail for up to 45 seconds.")
    Ingestor(db, embedder, ["parent_child"]).ingest(doc)
    [hit] = Retriever(db, embedder, strategy="parent_child").vector("PgBouncer follows", 1)
    assert hit.chunk_id.startswith("chunk:ops.child.")
    assert hit.parent_text == "Ops\nFailover\n\nPatroni promotes a replica. PgBouncer follows it. " \
                              "Writes fail for up to 45 seconds."
    assert hit.generation_text == hit.parent_text


def test_a_different_embedder_is_refused(corpus_db):
    from contextlayer.db import EmbedderMismatch
    from contextlayer.embeddings import HashEmbedder

    other_size = HashEmbedder(dim=128)
    same_size_other_model = HashEmbedder()
    same_size_other_model.name = "another-model"
    for embedder in (other_size, same_size_other_model):
        with pytest.raises(EmbedderMismatch):
            Retriever(corpus_db, embedder)
        with pytest.raises(EmbedderMismatch):
            Ingestor(corpus_db, embedder)


def test_relinking_replaces_links_in_one_go(db, embedder):
    ingestor = Ingestor(db, embedder)
    a = SourceDocument("a", "A", "See [[b|the B doc]].")
    ingestor.ingest_all([a, SourceDocument("b", "B", "Plain."), SourceDocument("c", "C", "Plain.")])
    a.content = "Now see [[c]] instead."
    assert ingestor.link(a) == 1
    assert db.one("SELECT VALUE out FROM links_to WHERE in = document:a") == [db.one("RETURN document:c")]
