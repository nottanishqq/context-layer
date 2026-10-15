"""Test fixtures: a throwaway SurrealDB server and a fresh database per test.

Tests run against a real `surreal` process (in-memory storage) because the
behaviour under test lives in SurrealQL. If SURREAL_TEST_URL is set, that
server is used instead of starting one.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from contextlayer.config import Settings
from contextlayer.db import Database
from contextlayer.embeddings import HashEmbedder

CORPUS = Path(__file__).parent.parent / "data" / "corpus"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def surreal_url():
    if url := os.environ.get("SURREAL_TEST_URL"):
        yield url
        return
    binary = shutil.which("surreal")
    if not binary:
        pytest.skip("the surreal binary is not installed")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "start", "--user", "root", "--pass", "root", "--bind", f"127.0.0.1:{port}", "memory"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("surreal did not start")
    yield f"ws://127.0.0.1:{port}/rpc"
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def settings(surreal_url) -> Settings:
    return dataclasses.replace(
        Settings(), surreal_url=surreal_url, surreal_user="root", surreal_pass="root",
        surreal_ns="test", surreal_db=f"t_{uuid.uuid4().hex[:10]}", embedder="hash",
    )


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder()


@pytest.fixture
def db(settings, embedder):
    with Database.connect(settings) as conn:
        conn.query(f"DEFINE DATABASE IF NOT EXISTS {settings.surreal_db}")
        conn.apply_schema(embedder)
        yield conn
        conn.query(f"REMOVE DATABASE {settings.surreal_db}")


@pytest.fixture
def corpus_db(db, embedder):
    from contextlayer.ingest import Ingestor, load_directory

    Ingestor(db, embedder, ["section"]).ingest_all(load_directory(CORPUS))
    return db
