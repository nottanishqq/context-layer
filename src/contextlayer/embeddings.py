"""Embedders turn text into vectors.

Everything else in the context layer depends only on the `Embedder` protocol,
so swapping models is a one-line change. Two implementations ship:

- `FastEmbedEmbedder`: a real local model (ONNX, no API key, ~130 MB download).
- `HashEmbedder`: a deterministic bag-of-words hash. It knows nothing about
  meaning, but shared words produce similar vectors, which is enough for
  tests and for running the course offline.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol, Sequence

from contextlayer.config import Settings


class Embedder(Protocol):
    name: str  # identifies the model; recorded in the database (see Database.check_embedder)
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed passages for storage."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (some models use a different prefix for queries)."""
        ...


class FastEmbedEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding

        self.name = model_name
        self._model = TextEmbedding(model_name)
        self.dim = len(next(iter(self._model.embed(["probe"]))))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()


_TOKEN = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@lru_cache(maxsize=4)
def get_embedder(kind: str | None = None, model_name: str | None = None) -> Embedder:
    """Build (and cache) the embedder named in settings."""
    settings = Settings()
    kind = kind or settings.embedder
    if kind == "hash":
        return HashEmbedder()
    if kind == "fastembed":
        return FastEmbedEmbedder(model_name or settings.embedding_model)
    raise ValueError(f"Unknown embedder {kind!r}; expected 'fastembed' or 'hash'")
