"""Settings, read from environment variables (and a `.env` file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # SurrealDB connection
    surreal_url: str = field(default_factory=lambda: _env("SURREAL_URL", "ws://127.0.0.1:8000/rpc"))
    surreal_user: str = field(default_factory=lambda: _env("SURREAL_USER", "root"))
    surreal_pass: str = field(default_factory=lambda: _env("SURREAL_PASS", "root"))
    surreal_ns: str = field(default_factory=lambda: _env("SURREAL_NS", "contextlayer"))
    surreal_db: str = field(default_factory=lambda: _env("SURREAL_DB", "main"))
    # Password for the read-only login text-to-SurrealQL runs as. Set it so every
    # process uses the same one; unset, each process picks a random one.
    viewer_pass: str = field(default_factory=lambda: _env("CL_VIEWER_PASS", ""))

    # Embeddings: "fastembed" (local ONNX model) or "hash" (deterministic, offline, for tests)
    embedder: str = field(default_factory=lambda: _env("CL_EMBEDDER", "fastembed"))
    embedding_model: str = field(default_factory=lambda: _env("CL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"))

    # Generation
    llm_model: str = field(default_factory=lambda: _env("CL_LLM_MODEL", "claude-opus-5-5"))
