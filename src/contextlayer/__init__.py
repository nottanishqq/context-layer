"""A context layer for AI agents, built on SurrealDB."""

from contextlayer.config import Settings
from contextlayer.context import ContextBuilder, ContextPacket
from contextlayer.db import Database
from contextlayer.embeddings import get_embedder
from contextlayer.ingest import Ingestor, load_directory
from contextlayer.memory import MemoryStore
from contextlayer.retrieval import Hit, Retriever

__all__ = [
    "ContextBuilder", "ContextPacket", "Database", "Hit", "Ingestor", "MemoryStore",
    "Retriever", "Settings", "get_embedder", "load_directory",
]
