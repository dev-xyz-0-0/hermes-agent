"""
Hermes Holographic Memory Web Viewer (FastAPI)

最后一个拼图：可视化记忆系统的 Dashboard、Fact 浏览器和 Entity 关系图。
对标 agentmemory 的 :3113 内置 viewer。

启动：
    cd ~/.hermes && python3 scripts/holographic_viewer.py

浏览器访问：
    http://localhost:3114
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PORT = int(os.environ.get("HERMES_VIEWER_PORT", "3114"))
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

# Insert paths for holographic plugin imports
_HERMES_AGENT = _HERMES_HOME / "hermes-agent"
for _p in [str(_HERMES_AGENT), str(_HERMES_AGENT / "plugins" / "memory")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("memviewer")

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from holographic.store import MemoryStore
from holographic.retrieval import FactRetriever

# ---------------------------------------------------------------------------
# DB 直连（避免 plugin 初始化的 overhead，直接读 SQLite）
# ---------------------------------------------------------------------------
DB_PATH = _HERMES_HOME / "memory_store.db"
BENCHMARK_PATH = _HERMES_HOME / "memory_benchmark.json"

app = FastAPI(title="Hermes Memory Viewer", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_store: MemoryStore | None = None
_retriever: FactRetriever | None = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore(db_path=str(DB_PATH))
    return _store


def get_retriever() -> FactRetriever:
    global _retriever
    if _retriever is None:
        _retriever = FactRetriever(store=get_store())
    return _retriever


def quick_conn() -> sqlite3.Connection | None:
    """Return a read-only connection for simple queries."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def api_health():
    """系统健康状态 + 基础统计"""
    conn = quick_conn()
    if conn is None:
        return {"status": "error", "message": f"DB not found at {DB_PATH}"}

    try:
        total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        relationships = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
        banks = conn.execute("SELECT COUNT(*) FROM memory_banks").fetchone()[0]
        # Category breakdown
        cats = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        # Trust distribution
        trust = conn.execute(
            "SELECT ROUND(trust_score * 10, 0) * 10 AS bucket, COUNT(*) as cnt "
            "FROM facts GROUP BY bucket ORDER BY bucket"
        ).fetchall()
        # Avg trust across all facts
        avg_trust_row = conn.execute(
            "SELECT ROUND(AVG(trust_score), 2) as avg_trust FROM facts"
        ).fetchone()
        # Recent facts
        recent = conn.execute(
            "SELECT fact_id, content, category, trust_score, created_at "
            "FROM facts ORDER BY fact_id DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    bench = {}
    if BENCHMARK_PATH.exists():
        try:
            bdata = json.loads(BENCHMARK_PATH.read_text())
            bench = {
                "r_at_5": bdata.get("methods", {}).get("all_5_default", {}).get("r_at_5", None),
                "generated_at": bdata.get("generated_at", ""),
                "test_cases": bdata.get("test_case_count", 0),
                "facts_total": bdata.get("facts_total", 0),
            }
        except Exception:
            bench = {"error": "parse failed"}

    return {
        "status": "ok",
        "db_path": str(DB_PATH),
        "db_size_mb": round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0,
        "facts_total": total,
        "entities_total": entities,
        "relationships_total": relationships,
        "memory_banks": banks,
        "categories": {r["category"]: r["cnt"] for r in cats},
        "trust_distribution": [{"bucket": r["bucket"], "count": r["cnt"]} for r in trust],
        "avg_trust": avg_trust_row["avg_trust"] if avg_trust_row else None,
        "recent_facts": [
            {"id": r["fact_id"], "content": r["content"][:100], "category": r["category"],
             "trust": r["trust_score"], "created": r["created_at"]}
            for r in recent
        ],
        "benchmark": bench if bench else None,
        "viewer_port": PORT,
    }


@app.get("/api/facts")
async def api_facts(
    category: str | None = None,
    min_trust: float = Query(0.0, ge=0.0, le=1.0),
    sort: str = Query("id", regex="^(id|trust|created|updated)$"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    tag: str | None = None,
):
    """分页浏览所有 facts

    Added `sort=updated` and `tag` filter (LIKE '%tag%' on tags column).
    """
    conn = quick_conn()
    if conn is None:
        return {"facts": [], "total": 0, "offset": offset}

    try:
        where = ["1=1"]
        params: list[Any] = []
        if category:
            where.append("category = ?")
            params.append(category)
        if min_trust > 0:
            where.append("trust_score >= ?")
            params.append(min_trust)
        if tag:
            where.append("tags LIKE ?")
            params.append(f"%{tag}%")

        sort_col = {"id": "fact_id", "trust": "trust_score", "created": "created_at", "updated": "updated_at"}[sort]
        sort_dir = "DESC" if order == "desc" else "ASC"

        total = conn.execute(
            f"SELECT COUNT(*) FROM facts WHERE {' AND '.join(where)}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, "
            f"retrieval_count, helpful_count, created_at, updated_at "
            f"FROM facts WHERE {' AND '.join(where)} "
            f"ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
    finally:
        conn.close()

    return {
        "facts": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/facts/by_day")
async def api_facts_by_day(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(20, ge=1, le=200),
):
    """Facts filtered by creation date (WHERE DATE(created_at) = ?)"""
    conn = quick_conn()
    if conn is None:
        return {"facts": [], "total": 0}

    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE DATE(created_at) = ?", (date,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT fact_id, content, category, tags, trust_score, "
            "retrieval_count, helpful_count, created_at, updated_at "
            "FROM facts WHERE DATE(created_at) = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (date, limit),
        ).fetchall()
    finally:
        conn.close()

    return {"facts": [dict(r) for r in rows], "total": total, "date": date}


@app.get("/api/facts/timeline")
async def api_facts_timeline(days: int = Query(30, ge=1, le=365)):
    """Aggregate fact timeline: returns [{date, count, avg_trust, sample_facts}]
    for the last N days. Each entry includes up to 5 sample fact IDs and content snippets.
    """
    conn = quick_conn()
    if conn is None:
        return {"timeline": []}

    try:
        rows = conn.execute(
            """SELECT DATE(created_at) as day,
                      COUNT(*) as count,
                      ROUND(AVG(trust_score), 2) as avg_trust
               FROM facts
               WHERE created_at >= DATE('now', ?)
               GROUP BY day
               ORDER BY day""",
            (f"-{days} days",),
        ).fetchall()

        timeline = []
        for r in rows:
            day = r["day"]
            samples = conn.execute(
                "SELECT fact_id, content FROM facts "
                "WHERE DATE(created_at) = ? ORDER BY trust_score DESC LIMIT 5",
                (day,),
            ).fetchall()
            timeline.append({
                "date": day,
                "count": r["count"],
                "avg_trust": r["avg_trust"],
                "sample_facts": [
                    {"fact_id": s["fact_id"], "content_preview": s["content"][:120]}
                    for s in samples
                ],
            })
    finally:
        conn.close()

    return {"timeline": timeline}


@app.get("/api/tiers")
async def api_tiers():
    """Aggregate tier statistics: per-tier counts/avg_trust + timeline.

    Returns:
      tiers: [{tier, count, avg_trust}] — summary per tier tag
      timeline: [{date, {tier: count}}]  — daily breakdown for last 30 days
    """
    conn = quick_conn()
    if conn is None:
        return {"tiers": [], "timeline": []}

    try:
        # Per-tier aggregates
        tier_rows = conn.execute(
            """SELECT TRIM(tags) as raw_tags,
                      COUNT(*) as cnt,
                      ROUND(AVG(trust_score), 2) as avg_trust
               FROM facts
               WHERE tags LIKE '%tier:%'
               GROUP BY raw_tags
               ORDER BY cnt DESC"""
        ).fetchall()

        # Aggregate by tier name (extract tier:xxx from tags)
        tiers_map: dict[str, dict] = {}
        for r in tier_rows:
            raw = r["raw_tags"] or ""
            for part in raw.split(","):
                part = part.strip()
                if part.startswith("tier:"):
                    tname = part.replace("tier:", "")
                    if tname not in tiers_map:
                        tiers_map[tname] = {"count": 0, "total_trust": 0.0}
                    tiers_map[tname]["count"] += r["cnt"]
                    tiers_map[tname]["total_trust"] += r["avg_trust"] * r["cnt"]

        tiers = []
        for tname, data in tiers_map.items():
            avg_trust = round(data["total_trust"] / data["count"], 2) if data["count"] else 0
            tiers.append({"tier": tname, "count": data["count"], "avg_trust": avg_trust})

        # Tier timeline (last 30 days)
        timeline_rows = conn.execute(
            """SELECT DATE(created_at) as day, tags
               FROM facts
               WHERE tags LIKE '%tier:%'
                 AND created_at >= DATE('now', '-30 days')
               ORDER BY day"""
        ).fetchall()

        day_buckets: dict[str, dict[str, int]] = {}
        for r in timeline_rows:
            day = r["day"]
            raw = r["tags"] or ""
            if day not in day_buckets:
                day_buckets[day] = {}
            for part in raw.split(","):
                part = part.strip()
                if part.startswith("tier:"):
                    tname = part.replace("tier:", "")
                    day_buckets[day][tname] = day_buckets[day].get(tname, 0) + 1

        timeline = [
            {"date": d, "tiers": day_buckets[d]}
            for d in sorted(day_buckets.keys())
        ]
    finally:
        conn.close()

    return {"tiers": tiers, "timeline": timeline}


@app.get("/api/facts/search")
async def api_search(
    q: str = Query(..., min_length=1),
    category: str | None = None,
    min_trust: float = Query(0.1, ge=0.0, le=1.0),
    limit: int = Query(20, ge=1, le=100),
):
    """混合检索"""
    retriever = get_retriever()
    try:
        results = retriever.search(q, category=category, min_trust=min_trust, limit=limit)
        return {"results": results, "count": len(results), "query": q}
    except Exception as e:
        return JSONResponse({"error": str(e), "results": []}, status_code=500)


@app.get("/api/entities")
async def api_entities(
    min_facts: int = Query(1, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """列出所有 entity，附带 fact 数量"""
    conn = quick_conn()
    if conn is None:
        return {"entities": []}

    try:
        rows = conn.execute(
            """SELECT e.entity_id, e.name, e.entity_type,
                      COUNT(fe.fact_id) as fact_count
               FROM entities e
               LEFT JOIN fact_entities fe ON fe.entity_id = e.entity_id
               GROUP BY e.entity_id
               HAVING fact_count >= ?
               ORDER BY fact_count DESC, e.name
               LIMIT ?""",
            (min_facts, limit),
        ).fetchall()
    finally:
        conn.close()

    return {"entities": [dict(r) for r in rows], "total": len(rows)}


@app.get("/api/entities/{entity_id}/graph")
async def api_entity_graph(entity_id: int, depth: int = Query(2, ge=1, le=4)):
    """Entity 关系图谱"""
    store = get_store()
    try:
        graph = store.get_graph(f"__id__:{entity_id}", depth=depth)
        return graph
    except Exception as e:
        return JSONResponse({"error": str(e), "nodes": [], "edges": []}, status_code=500)


@app.get("/api/entities/{name}/probe")
async def api_probe(name: str, limit: int = Query(10, ge=1, le=50)):
    """Probe an entity by name"""
    retriever = get_retriever()
    try:
        results = retriever.probe(name, limit=limit)
        return {"results": results, "entity": name, "count": len(results)}
    except Exception as e:
        return JSONResponse({"error": str(e), "results": []}, status_code=500)


@app.get("/api/categories")
async def api_categories():
    """分类统计"""
    conn = quick_conn()
    if conn is None:
        return {"categories": []}

    try:
        rows = conn.execute(
            """SELECT category,
                      COUNT(*) as fact_count,
                      ROUND(AVG(trust_score), 2) as avg_trust,
                      ROUND(AVG(retrieval_count), 1) as avg_retrieval,
                      ROUND(AVG(helpful_count), 1) as avg_helpful
               FROM facts
               GROUP BY category
               ORDER BY fact_count DESC"""
        ).fetchall()
    finally:
        conn.close()

    return {"categories": [dict(r) for r in rows]}


@app.get("/api/timeline")
async def api_timeline():
    """事实创建时间分布（按天）"""
    conn = quick_conn()
    if conn is None:
        return {"days": []}

    try:
        rows = conn.execute(
            """SELECT DATE(created_at) as day,
                      COUNT(*) as count,
                      ROUND(AVG(trust_score), 2) as avg_trust
               FROM facts
               GROUP BY day
               ORDER BY day"""
        ).fetchall()
    finally:
        conn.close()

    return {"days": [dict(r) for r in rows]}


@app.get("/api/activity")
async def api_activity(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    op_type: str | None = Query(None, pattern="^(all|add|update)$"),
):
    """Live activity feed — facts ordered by updated_at desc.
    
    op_type: 'all' (default, shows everything), 'add' (created=updated, i.e. never modified),
             'update' (created != updated, i.e. modified after creation)
    """
    conn = quick_conn()
    if conn is None:
        return {"activities": [], "total": 0}

    try:
        where = ["1=1"]
        params: list[Any] = []
        if op_type == "add":
            where.append("created_at = updated_at")
        elif op_type == "update":
            where.append("created_at != updated_at")

        total = conn.execute(
            f"SELECT COUNT(*) FROM facts WHERE {' AND '.join(where)}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, "
            f"retrieval_count, helpful_count, created_at, updated_at "
            f"FROM facts WHERE {' AND '.join(where)} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
    finally:
        conn.close()

    return {
        "activities": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/benchmark")
async def api_benchmark():
    """基准测试结果"""
    if not BENCHMARK_PATH.exists():
        return {"benchmark": None}

    try:
        data = json.loads(BENCHMARK_PATH.read_text())
        # Only return summary, not full failures list (too large)
        if "methods" in data:
            for m in data["methods"]:
                m_data = data["methods"][m]
                if isinstance(m_data, dict):
                    m_data.pop("failures", None)
        return data
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Static Frontend
# ---------------------------------------------------------------------------

_HTML: str | None = None


@app.get("/", response_class=HTMLResponse)
async def index():
    global _HTML
    if _HTML is None:
        index_path = Path(__file__).parent.parent / "skills" / "development" / "hermes-memory" / "viewer" / "index.html"
        alt_path = Path(__file__).parent / "viewer" / "index.html"
        for p in [index_path, alt_path]:
            if p.exists():
                _HTML = p.read_text(encoding="utf-8")
                break
        if _HTML is None:
            _HTML = "<html><body><h1>Viewer not found</h1></body></html>"
    return HTMLResponse(_HTML)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\n  🧠 Hermes Holographic Memory Viewer")
    print(f"  ───────────────────────────────────")
    print(f"  Dashboard:  http://localhost:{PORT}")
    print(f"  API:        http://localhost:{PORT}/api/health")
    print(f"  DB:         {DB_PATH}")
    print(f"  Port:       {PORT} (set HERMES_VIEWER_PORT to change)\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
