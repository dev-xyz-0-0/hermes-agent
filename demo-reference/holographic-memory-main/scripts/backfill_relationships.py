#!/usr/bin/env python3
"""
Backfill: 从已有记忆中提取实体关系，填充关系图谱。

用法：
    python3 scripts/backfill_relationships.py           # 处理全部事实
    python3 scripts/backfill_relationships.py --dry-run  # 预览不改库
    python3 scripts/backfill_relationships.py --limit 500  # 只处理最近500条
"""

import json, os, sys, time, argparse
from datetime import datetime

# 复用 holographic memory 的 store
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from holographic_memory import MemoryStore
from holographic_memory.store import _DEDUP_THRESHOLD

def main():
    parser = argparse.ArgumentParser(description="从已有记忆中提取实体关系")
    parser.add_argument("--dry-run", action="store_true", help="只预览不改库")
    parser.add_argument("--limit", type=int, default=0, help="处理条数上限（0=全部）")
    parser.add_argument("--batch", type=int, default=50, help="每批条数")
    parser.add_argument("--api-key", help="LLM API key（默认用 DEEPSEEK_API_KEY）")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("❌ 需要 DEEPSEEK_API_KEY 环境变量或 --api-key 参数")
        sys.exit(1)

    store = MemoryStore()
    conn = store._conn

    # 获取已有事实
    total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    existing_rels = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
    print(f"📊 已有事实: {total} 条")
    print(f"📊 已有关系: {existing_rels} 条")
    print(f"📊 已有实体: {conn.execute('SELECT COUNT(*) FROM entities').fetchone()[0]} 个")

    query = "SELECT fact_id, content, category FROM facts ORDER BY fact_id DESC"
    if args.limit > 0:
        query += f" LIMIT {args.limit}"
    rows = conn.execute(query).fetchall()

    print(f"\n🔄 将要处理: {len(rows)} 条事实\n")
    if args.dry_run:
        print("🏁 --dry-run 模式，不改库")
        return

    # 分批调用 LLM
    batch = []
    processed = 0
    relations_found = 0

    for row in rows:
        batch.append(row)
        if len(batch) >= args.batch:
            relations_found += process_batch(batch, store, api_key, args.dry_run)
            processed += len(batch)
            print(f"  → 已处理 {processed}/{len(rows)}, 共发现 {relations_found} 条关系", end="\r")
            batch = []
            time.sleep(0.5)  # 避免 API 限流

    if batch:
        relations_found += process_batch(batch, store, api_key, args.dry_run)
        processed += len(batch)

    print(f"\n\n✅ 完成！处理 {processed} 条事实，提取 {relations_found} 条关系")
    
    # 最终统计
    final_rels = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
    print(f"📊 关系总计: {final_rels} 条（之前 {existing_rels} 条，新增 {final_rels - existing_rels} 条）")


def process_batch(rows, store, api_key, dry_run):
    """调用 LLM 从一批事实中提取关系三元组。"""
    import urllib.request

    texts = [f"{r['fact_id']}: {r['content'][:200]}" for r in rows]
    prompt = (
        "从以下事实列表中提取实体关系三元组。\n"
        "关注：人物-组织关系、人员-项目关系、工具-用途关系、概念-概念关系。\n\n"
        "事实列表：\n" + "\n".join(texts) + "\n\n"
        "输出 JSON 数组，每项格式：\n"
        '{"source": "实体名", "relation": "关系类型", "target": "实体名"}\n\n'
        "只输出 JSON，不要其他文字。"
    )

    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }).encode()

    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        content = result["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            content = content.rsplit("```", 1)[0]
        triples = json.loads(content.strip())
    except Exception as e:
        print(f"\n⚠️ LLM 调用失败: {e}")
        return 0

    if not isinstance(triples, list):
        return 0

    if dry_run:
        return len(triples)

    count = 0
    for t in triples:
        try:
            source = str(t.get("source", "")).strip()[:100]
            relation = str(t.get("relation", "related_to")).strip()[:100]
            target = str(t.get("target", "")).strip()[:100]
            if source and target and source != target:
                src_id = store._resolve_entity(source)
                tgt_id = store._resolve_entity(target)
                store._add_relationship(src_id, tgt_id, relation)
                count += 1
        except Exception:
            pass

    return count


if __name__ == "__main__":
    main()
