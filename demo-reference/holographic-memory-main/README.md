# Holographic Memory

本地优先、SQLite 持久化的 AI Agent 记忆引擎。零外部依赖，无需安装数据库或向量服务。

## 功能速览

| 能力 | 说明 |
|------|------|
| 存储 | SQLite 持久化事实存储，自动 WAL 模式 |
| 检索 | FTS5 全文搜索 + Jaccard 重叠度重排 + 可选 HRR 相位向量（numpy） |
| 信任评分 | 每条事实有 trust_score 0-1，反馈训练影响权重 |
| 隐私过滤 | 8 种正则自动拦截 API Key / Token / 密码 / 电话 / 邮箱 |
| 语义去重 | N-gram Jaccard 相似度检测，近重复自动合并提升信任 |
| 实体抽取 | 正则提取命名实体（大写短语/引号/AKA）+ 实体消歧 |
| 关系图谱 | 实体间关系三元组存储，支持 BFS 邻域查询 |
| 时间衰减 | Ebbinghaus 式遗忘曲线（`temporal_decay_half_life`） |
| 版本化 | Supersede 标记：旧事实 tags 标注 `superseded_by:{new_id}` |
| 批量导入 | `add_facts_batch()` 事务写入，跳过逐条抽取 |
| 权重学习 | `learn_weights_from_feedback()` 根据反馈数据调整检索权重 |

## 快速开始

```bash
pip install -e ".[all]"
```

```python
from holographic_memory import MemoryStore, FactRetriever

store = MemoryStore(db_path="memory.db")
retriever = FactRetriever(store)

# 存储事实
fid = store.add_fact("用户使用 PyCharm 作为 Python IDE", category="user_pref")

# 检索（三因子混合）
results = retriever.search("PyCharm Python IDE", limit=5)

# 实体探针
results = retriever.probe("PyCharm", limit=5)

# 多实体组合推理
results = retriever.reason(["PyCharm", "Python"], limit=5)
```

## 工具

### MCP 服务器（供 Claude Code / Codex 使用）

```bash
python3 scripts/holographic_mcp_server.py
```

向任何 MCP 兼容的 Agent 暴露 15 个记忆工具：add、search、probe、reason、related、contradict、update、remove、list 等。

### Web Dashboard

```bash
python3 scripts/holographic_viewer.py
# 浏览器打开 http://localhost:3114
```

可视化浏览 Facts、Entities、关系图谱。支持按类别/信任分/时间筛选。

### 基准测试

```bash
# 召回率评测（10 个固定 query）
python3 scripts/recall_benchmark.py

# 完整基准（100 个随机用例，输出 R@5 各维度分析）
python3 scripts/holographic_benchmark.py

# 数据库健康诊断
python3 scripts/holographic_memory_health.py
```

### 关系图谱回填（单次操作）

从已有事实中通过 LLM 批量提取实体关系：

```bash
python3 scripts/backfill_relationships.py
```

## 两层架构

本仓库同时维护两个版本的核心引擎：

| 版本 | 位置 | 说明 |
|------|------|------|
| **standalone 版** | `src/holographic_memory/` | 零外部依赖，pip install 即用 |
| **Hermes Agent 插件版** | `reference/hermes-agent-plugin/` | 完整功能，需配合 Hermes Agent 使用 |

插件版额外包含：四层记忆管线（Working→Episodic→Semantic→Procedural）、Cross-Encoder reranker（bge-reranker-v2-m3）、BGE/OpenAI 多后端嵌入、LLM 驱动的会话事实/关系自动抽取、`on_memory_write` 内置记忆镜像。

## 安装

```bash
git clone https://github.com/bysc1000/holographic-memory.git
cd holographic-memory
pip install -e ".[all]"    # 含 numpy + fastapi + fastmcp
pip install -e "."         # 仅核心（无外部依赖）
```

依赖分组：

| 分组 | 包含 |
|------|------|
| 无（默认） | 纯 Python + SQLite |
| `[numpy]` | HRR 向量加速 |
| `[fastapi]` | Web Dashboard |
| `[mcp]` | MCP 服务器 |
| `[all]` | 全选 |

## 检索评分

```
relevance = FTS5_rank × w_fts + Jaccard × w_jac + HRR_sim × w_hrr
score = relevance × trust_score
score *= 0.5 ^ (age_days / half_life)    # 可选
```

默认权重：FTS5=0.4, Jaccard=0.3, HRR=0.3（无 numpy 时 FTS5=0.6, Jaccard=0.4, HRR=0.0）

权重可通过 `FactRetriever(fts_weight=..., jaccard_weight=..., hrr_weight=...)` 自定义，或通过 `retriever.learn_weights_from_feedback()` 从数据学习。

## 数据库 Schema

```sql
facts (fact_id PK, content UNIQUE, category, tags, trust_score,
       retrieval_count, helpful_count, created_at, updated_at, hrr_vector)

entities (entity_id PK, name, entity_type, aliases, created_at)

fact_entities (fact_id FK, entity_id FK)          -- 事实-实体映射

entity_relationships (source_id FK, target_id FK, type, strength)  -- 关系图谱

facts_fts (VIRTUAL fts5)                            -- 全文搜索索引

memory_banks (bank_name, vector BLOB, dim)         -- 分类 HRR 向量池
```

## 开源协议

MIT
