"""
Holographic Memory — standalone entry point.

Core classes for use outside Hermes Agent:

    from holographic_memory import MemoryStore, FactRetriever

    store = MemoryStore(db_path="memory.db")
    retriever = FactRetriever(store)
    results = retriever.search("query")
"""

from .store import MemoryStore
from .retrieval import FactRetriever
from .holographic import (
    encode_atom,
    encode_text,
    bind,
    unbind,
    bundle,
)

__all__ = [
    "MemoryStore",
    "FactRetriever",
    "encode_atom",
    "encode_text",
    "bind",
    "unbind",
    "bundle",
]
