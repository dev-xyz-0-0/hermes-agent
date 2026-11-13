#!/usr/bin/env python3
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

home = Path.home() / ".hermes"
db_path = home / "memory_store.db"
state_path = home / "state-snapshots" / "holographic_memory_health.json"

def count_table(conn, table):
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return None

def main():
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    report = {
        "time": now,
        "database": str(db_path),
        "integrity": "missing",
        "facts_count": 0,
        "facts_delta": None,
        "entities_count": 0,
        "conflict_check": {},
    }

    if not db_path.exists():
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    conn = sqlite3.connect(db_path)
    try:
        report["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        report["facts_count"] = count_table(conn, "facts") or 0
        report["entities_count"] = count_table(conn, "entities") or 0
        report["fact_entities_count"] = count_table(conn, "fact_entities") or 0
        report["fts_count"] = count_table(conn, "facts_fts")

        low_trust = conn.execute("SELECT COUNT(*) FROM facts WHERE trust_score <= 0.2").fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE updated_at IS NOT NULL AND updated_at < datetime('now', '-180 days')"
        ).fetchone()[0]
        contradiction_markers = conn.execute(
            """
            SELECT COUNT(*) FROM facts
            WHERE lower(content) LIKE '% conflict %'
               OR lower(content) LIKE '% contradict%'
               OR content LIKE '%冲突%'
               OR content LIKE '%矛盾%'
            """
        ).fetchone()[0]
        report["conflict_check"] = {
            "low_trust_facts": low_trust,
            "stale_facts_180d": stale,
            "facts_with_conflict_markers": contradiction_markers,
            "status": "review" if low_trust or contradiction_markers else "clear",
        }
    finally:
        conn.close()

    previous = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    previous_count = previous.get("facts_count")
    if isinstance(previous_count, int):
        report["facts_delta"] = report["facts_count"] - previous_count

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
