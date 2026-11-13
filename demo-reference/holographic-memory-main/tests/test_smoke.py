"""Smoke tests for holographic memory core."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))

from holographic_memory.store import MemoryStore
from holographic_memory.retrieval import FactRetriever
import tempfile, json

def test_add_and_retrieve():
    """最基本的写入+检索测试"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        store = MemoryStore(db_path=db_path)
        fid = store.add_fact("Python是一种编程语言", category="general", tags="python,language")
        assert fid > 0, "add_fact should return positive fact_id"

        retriever = FactRetriever(store)
        results = retriever.search("Python", limit=5)
        assert len(results) > 0, "search should find the fact"
        assert any("编程语言" in r.get('content', '') for r in results)

        # probe entity
        results = retriever.probe("Python", limit=5)
        assert len(results) > 0, "probe should find the entity"
    finally:
        os.unlink(db_path)

def test_privacy_filter():
    """隐私过滤不应存储API key"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    try:
        store = MemoryStore(db_path=db_path)
        result = store.add_fact("My API key is sk-abc123")
        assert result is None, "API keys should be filtered"
    finally:
        os.unlink(db_path)

if __name__ == '__main__':
    test_add_and_retrieve()
    test_privacy_filter()
    print("✅ All tests passed")
