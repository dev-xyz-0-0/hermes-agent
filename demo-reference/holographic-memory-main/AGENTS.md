# Hermes Holographic Memory — Agent 入手指南

> 给 Claude Code / Codex / Cursor 等 Coding Agent 的快速参考。
> 完整项目文档见 [README.md](./README.md)。

---

## 项目定位

Hermes Agent 的可选记忆后端，通过 `memory.provider: holographic` 启用，功能对标 agentmemory。
一个 **纯 SQLite、零外部依赖、不依赖 numpy 也可运行** 的持久化记忆引擎。

---

## 核心文件

```
plugins/memory/holographic/
├── __init__.py          # 主入口 — HolographicMemoryProvider (MemoryProvider ABC)
│                        #   prefetch/enhanced_prefetch/sync_turn/session_end
│                        #   fact_store (15 actions) + fact_feedback (2 actions)
├── store.py             # 存储引擎 — SQLite 写入/读取/隐私过滤/实体提取/图谱构建/Supersede
│                        #   6 张表：facts, entities, fact_entities, facts_fts (FTS5),
│                        #   memory_banks, entity_relationships
├── retrieval.py         # 检索引擎 — 三因子混合检索 + probe/reason/related/contradict + Cross-Encoder
│                        #   FactRetriever, CrossEncoderReranker
├── holographic.py       # HRR 向量代数 — encode_atom/encode_text/bind/unbind/bundle
│                        #   numpy 可选，无 numpy 时自动回退 FTS5
├── consolidation.py     # 记忆整合 — 规则提取(中英29模式)/重复检测(Jaccard)/低信任修剪
├── promotion.py         # 四层管线 — Working→Episodic→Semantic→Procedural (LLM 驱动摘要)
├── llm_extractor.py     # LLM 事实提取 — OpenAI 兼容 API / session_end 自动触发
├── embeddings.py        # 多后端嵌入 — HRR(1024d) / OpenAI(512d) / BGE(512d) + 工厂模式
├── cli.py               # CLI 交互命令
├── plugin.yaml          # Hermes 插件注册
└── test_holographic.py  # 63 个 pytest 用例

~/.hermes/scripts/
├── holographic_mcp_server.py    # MCP 服务器 (15 个工具, stdio 传输)
├── holographic_benchmark.py     # R@5 基准测试 (100 个用例)
└── holographic_memory_health.py # 数据库健康诊断
```

### 独立脚本

```
scripts/
├── holographic_mcp_server.py   # MCP 服务器（stdio 传输）
└── holographic_viewer.py       # FastAPI Web Dashboard
```

---

## 关键设计决策

1. **不依赖 numpy 也能运行**
   - 所有 HRR 操作有 numpy 时加速，无 numpy 时回退到 FTS5 + Jaccard
   - 检索权重自动重分配：FTS5=0.6, Jaccard=0.4, HRR=0.0
   - 绝不允许用 `pip install numpy` 来"修复"问题

2. **纯 SQLite，零外部依赖**
   - 无需 PostgreSQL / Redis / Pinecone / Milvus
   - WAL 模式，NFS/SMB/FUSE 自动回退
   - Profile 隔离：不同 Hermes profile 各自独立 `memory_store.db`

3. **三因子混合检索 + 自动降级**
   - FTS5(0.4) > Jaccard(0.3) > HRR(0.3) （无 numpy 时 HRR→0）
   - BGE 语义向量和 Graph BFS 作为独立的实体级操作（probe/reason/related/get_graph），不参与 search() 评分公式
   - 可选 Cross-Encoder reranker 作为搜索后精排

4. **Supersede 版本化机制**
   - 写入新事实时自动检测相似已有事实（FTS5 匹配）
   - 旧事实 tags 标记 `superseded_by:{new_id}`
   - 新事实 tags 标记 `supersedes:{old_id}`
   - **不要移除这个机制** — 它是 P2 级核心功能

5. **四层记忆管线**
   - Working（默认，单会话）→ Episodic（会话摘要，LLM 驱动）→ Semantic（跨会话稳定，≥2次）→ Procedural（重复模式，≥3次）
   - 每个层级通过 `tags: tier:xxx` 标记
   - retrieval 时通过 `tier` 参数过滤

6. **隐私过滤不能随意改动**
   - 8 种正则在 `store.py` 的 `_privacy_filter()` 中
   - 拦截：API Keys (sk-/ghp_/gho_等)、Bearer Token、邮箱、电话、信用卡、密码、Token/Key 模式
   - 存储前拦截，抛出 `PrivacyBlockedError`

---

## 类调用关系

```
HolographicMemoryProvider (__init__.py)
│
├── MemoryStore (store.py)
│   ├── SQLite connection
│   ├── EmbeddingBackend (embeddings.py → HRR/OpenAI/BGE)
│   ├── HRR module (holographic.py → encode_atom/bind/unbind/bundle)
│   ├── _privacy_filter() — 8 种正则
│   ├── _extract_entities() — 4 种正则
│   ├── _find_similar_facts() — Supersede 检测
│   ├── _compute_hrr_vector() / _rebuild_bank()
│   └── get_graph() — entity_relationships BFS
│
├── FactRetriever (retrieval.py)
│   ├── search() — FTS5 candidates → Jaccard rerank → HRR sim → trust × decay
│   ├── probe() — HRR 实体探针 (unbind role_entity from fact)
│   ├── reason() — 多实体 AND (min similarity across entities)
│   ├── related() — 结构邻域 (unbind bare atom from fact)
│   ├── contradict() — 实体重叠 × 内容分歧
│   └── CrossEncoderReranker (可选) — BAAI/bge-reranker-v2-m3
│
├── MemoryConsolidator (consolidation.py)
│   ├── consolidate_session() — 规则提取 → add_fact
│   ├── find_duplicates() — Jaccard ≥ threshold
│   └── prune_low_trust() — trust ≤ threshold × age > max_age
│
├── TierPromoter (promotion.py)
│   ├── promote_session() — working → episodic (LLM摘要)
│   ├── promote_episodic_to_semantic() — ≥2次出现 + 信任≥0.7
│   └── promote_semantic_to_procedural() — ≥3次出现 + 信任≥0.85
│
└── LLMFactExtractor (llm_extractor.py)
    └── extract_from_conversation() — LLM API → JSON facts
```

---

## 数据库 Schema

```sql
facts (fact_id PK, content UNIQUE, category, tags, trust_score,
       retrieval_count, helpful_count, created_at, updated_at, hrr_vector BLOB)

entities (entity_id PK, name, entity_type, aliases, created_at)

fact_entities (fact_id FK, entity_id FK, PK(fact_id, entity_id))

entity_relationships (rel_id PK, source_entity_id FK, target_entity_id FK,
                      relationship_type, strength, created_at, updated_at)

facts_fts (VIRTUAL TABLE USING fts5(content, tags, content=facts, content_rowid=fact_id))
  -- 自动同步：INSERT/UPDATE/DELETE 触发器

memory_banks (bank_id PK, bank_name UNIQUE, vector BLOB, dim, fact_count, updated_at)
```

---

## 检索评分公式

```
# 第1步：FTS5 获取候选池 (limit × 3)
# 第2步：多因子评分
relevance = FTS5_rank * w_fts + Jaccard * w_jac + HRR_sim * w_hrr
score = relevance * trust_score
# 第3步：时间衰减（可选）
score *= 0.5 ^ (age_in_days / half_life)
# 第4步：Cross-Encoder 精排（可选）
```

---

## 改进时需遵守的原则

1. **不要引入外部 DB 依赖** — 保持纯 SQLite，绝不引入 PostgreSQL/Redis/Pinecone
2. **确保 numpy 可选** — 所有 HRR 操作必须有无 numpy 的 fallback（FTS5 搜索）
3. **SQLite 线程安全** — 所有写操作通过 `self._lock = threading.RLock()` 保护
4. **隐私过滤不可改** — 8 种正则模式在 `store.py` 的 `_privacy_filter()` 中，删除/修改需要评估安全影响
5. **测试覆盖** — 修改检索逻辑后运行 `holographic_benchmark.py` 确认 R@5 不降低
6. **HRR 向量不可变** — `encode_atom()` 使用 SHA-256 确定性生成，保证跨进程/跨机器可重现
7. **异步安全** — `MCP server` 使用 stdio 传输 (stdout=JSON-RPC, stderr=log)，不启动 HTTP 服务器
8. **config.yaml 是唯一配置源** — 优先读 `~/.hermes/config.yaml` 下的 `plugins.hermes-memory-store`
9. **热加载不要做** — memory store 在 `initialize()` 时创建连接，生命周期内不重新加载
10. **中文兼容** — FTS5 CJK bigram 不需要额外分词器，实体提取正则支持中文

---

## 常用调试命令

```bash
# 1. 运行单元测试
cd $PROJECT_ROOT && source .venv/bin/activate
python -m pytest plugins/memory/holographic/test_holographic.py -v

# 2. 运行基准测试（必须在 hermes-agent 目录下）
python3 scripts/holographic_benchmark.py

# 3. 健康诊断
python3 scripts/holographic_memory_health.py

# 4. 直接查数据库
sqlite3 ~/.hermes/memory_store.db
.tables
SELECT category, COUNT(*) FROM facts GROUP BY category;
SELECT COUNT(*) FROM entity_relationships;

# 5. 查看事实详情
SELECT fact_id, substr(content, 1, 60), trust_score, category
FROM facts ORDER BY trust_score DESC LIMIT 10;

# 6. 查看关系图谱
SELECT e1.name, er.relationship_type, e2.name, er.strength
FROM entity_relationships er
JOIN entities e1 ON er.source_entity_id = e1.entity_id
JOIN entities e2 ON er.target_entity_id = e2.entity_id
LIMIT 20;

# 7. 启动 MCP 服务器（测试用）
python3 scripts/holographic_mcp_server.py

# 8. 手动测试添加事实
python3 -c "
import sys; sys.path.insert(0, '$HOME/.hermes/hermes-agent')
from plugins.memory.holographic.store import MemoryStore
s = MemoryStore()
fid = s.add_fact('测试事实', category='general')
print(f'Added fact #{fid}')
results = s.search_facts('测试')
for r in results: print(f'  [{r[\"trust_score\"]}] {r[\"content\"]}')
"
```

---

## 与 agentmemory 差距备忘

只差一项：**Web Viewer** (agentmemory 有 `:3113` 仪表盘)。  
其余 P0/P1/P2/P3 核心功能全部补齐，且有 8 项领先功能。

Hermes 领先的功能（agentmemory 没有）：
- 实体关系图谱 (entity_relationships + BFS)
- 不对称信任评分 (+0.05/-0.10)
- 隐私过滤 (8 种模式)
- 多嵌入后端 (HRR/OpenAI/BGE)
- Profile 级隔离 (多 profile 独立 DB)
- CJK 中文优化 (bigram + 中文实体提取)
- Cross-Encoder reranker (BGE reranker v2-m3)
- 矛盾检测 (contradict tool)
