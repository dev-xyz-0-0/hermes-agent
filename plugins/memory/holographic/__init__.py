"""hermes-memory-store — holographic memory plugin using MemoryProvider interface.

Registers as a MemoryProvider plugin, giving the agent structured fact storage
with entity resolution, trust scoring, and HRR-based compositional retrieval.

Original plugin by dusterbloom (PR #2351), adapted to the MemoryProvider ABC.

Config in $HERMES_HOME/config.yaml (profile-scoped):
  plugins:
    hermes-memory-store:
      db_path: $HERMES_HOME/memory_store.db   # omit to use the default
      auto_extract: false
      default_trust: 0.5
      min_trust_threshold: 0.3
      temporal_decay_half_life: 0
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from .store import MemoryStore
from .retrieval import FactRetriever
from . import holographic as hrr

logger = logging.getLogger(__name__)
_NUMPY_DEGRADED_MESSAGE = (
    "Holographic memory is running in degraded mode because numpy is not installed; "
    "HRR semantic retrieval, related/reason/contradict operations, and contradiction "
    "detection are disabled or fall back to FTS5 keyword search. Install numpy in the "
    "Hermes environment to enable full holographic memory."
)

# ---------------------------------------------------------------------------
# Tool schemas (unchanged from original PR)
# ---------------------------------------------------------------------------

FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. "
        "Use alongside the memory tool — memory for always-on context, "
        "fact_store for deep recall and compositional queries.\n\n"
        "ACTIONS (simple → powerful):\n"
        "• add — Store a fact the user would expect you to remember.\n"
        "• search — Keyword lookup ('editor config', 'deploy process').\n"
        "• probe — Entity recall: ALL facts about a person/thing.\n"
        "• related — What connects to an entity? Structural adjacency.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n"
        "• update/remove/list — CRUD operations.\n\n"
        "IMPORTANT: Before answering questions about the user, ALWAYS probe or reason first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "search", "probe", "related", "reason", "contradict", "update", "remove", "list"],
            },
            "content": {"type": "string", "description": "Fact content (required for 'add')."},
            "query": {"type": "string", "description": "Search query (required for 'search')."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason'."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'update'/'remove'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": (
        "Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. "
        "This trains the memory — good facts rise, bad facts sink."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["helpful", "unhelpful"]},
            "fact_id": {"type": "integer", "description": "The fact ID to rate."},
        },
        "required": ["action", "fact_id"],
    },
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    from hermes_constants import get_hermes_home
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        with open(config_path) as f:
            all_config = yaml.safe_load(f) or {}
        return all_config.get("plugins", {}).get("hermes-memory-store", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HolographicMemoryProvider(MemoryProvider):
    """Holographic memory with structured facts, entity resolution, and HRR retrieval."""

    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._store = None
        self._retriever = None
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))
        self._warned_numpy_degraded = False

    @property
    def name(self) -> str:
        return "holographic"

    def is_available(self) -> bool:
        return True  # SQLite is always available, numpy is optional

    def save_config(self, values, hermes_home):
        """Write config to config.yaml under plugins.hermes-memory-store."""
        from pathlib import Path
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml
            existing = {}
            if config_path.exists():
                with open(config_path) as f:
                    existing = yaml.safe_load(f) or {}
            existing.setdefault("plugins", {})
            existing["plugins"]["hermes-memory-store"] = values
            with open(config_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False)
        except Exception:
            pass

    def get_config_schema(self):
        from hermes_constants import display_hermes_home
        _default_db = f"{display_hermes_home()}/memory_store.db"
        return [
            {"key": "db_path", "description": "SQLite database path", "default": _default_db},
            {"key": "auto_extract", "description": "Auto-extract facts at session end", "default": "false", "choices": ["true", "false"]},
            {"key": "default_trust", "description": "Default trust score for new facts", "default": "0.5"},
            {"key": "hrr_dim", "description": "HRR vector dimensions", "default": "1024"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        _hermes_home = str(get_hermes_home())
        _default_db = _hermes_home + "/memory_store.db"
        db_path = self._config.get("db_path", _default_db)
        # Expand $HERMES_HOME in user-supplied paths so config values like
        # "$HERMES_HOME/memory_store.db" or "~/.hermes/memory_store.db" both
        # resolve to the active profile's directory.
        if isinstance(db_path, str):
            db_path = db_path.replace("$HERMES_HOME", _hermes_home)
            db_path = db_path.replace("${HERMES_HOME}", _hermes_home)
        default_trust = float(self._config.get("default_trust", 0.5))
        hrr_dim = int(self._config.get("hrr_dim", 1024))
        hrr_weight = float(self._config.get("hrr_weight", 0.3))
        temporal_decay = int(self._config.get("temporal_decay_half_life", 0))
        if not hrr._HAS_NUMPY and not self._warned_numpy_degraded:
            logger.warning(_NUMPY_DEGRADED_MESSAGE)
            self._warned_numpy_degraded = True

        self._store = MemoryStore(db_path=db_path, default_trust=default_trust, hrr_dim=hrr_dim)
        self._retriever = FactRetriever(
            store=self._store,
            temporal_decay_half_life=temporal_decay,
            hrr_weight=hrr_weight,
            hrr_dim=hrr_dim,
        )
        self._session_id = session_id

        # Warn if numpy is missing — HRR operations silently disabled (#17350)
        try:
            from . import holographic as _hrr
            self._hrr_available = _hrr._HAS_NUMPY
        except ImportError:
            self._hrr_available = False

        if not self._hrr_available:
            logger.warning(
                "Holographic memory: numpy not found. HRR vector operations disabled. "
                "Only FTS5 keyword search will be available. "
                "Install numpy to enable compositional retrieval (probe/reason/contradict)."
            )

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            total = self._store._conn.execute(
                "SELECT COUNT(*) FROM facts"
            ).fetchone()[0]
        except Exception:
            total = 0

        hrr_status = ""
        if not self._hrr_available:
            hrr_status = (
                "\nWARNING: numpy not installed — HRR vector operations disabled. "
                "Only keyword search works. probe/reason/contradict will use FTS5 fallback. "
                "Install numpy for full compositional retrieval."
            )

        if total == 0:
            return (
                "# Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                "Use fact_feedback to rate facts after using them (trains trust scores)."
                + hrr_status
            )
        return (
            f"# Holographic Memory\n"
            f"Active. {total} facts stored with entity resolution and trust scoring.\n"
            f"Use fact_store to search, probe entities, reason across entities, or add facts.\n"
            f"Use fact_feedback to rate facts after using them (trains trust scores)."
            + hrr_status
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._retriever or not query:
            return ""
        try:
            results = self._retriever.search(query, min_trust=self._min_trust, limit=5)
            if not results:
                return ""
            lines = []
            for r in results:
                trust = r.get("trust_score", r.get("trust", 0))
                lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
            return "## Holographic Memory\n" + "\n".join(lines)
        except Exception as e:
            logger.debug("Holographic prefetch failed: %s", e)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Holographic memory stores explicit facts via tools, not auto-sync.
        # The on_session_end hook handles auto-extraction if configured.
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "fact_store":
            return self._handle_fact_store(args)
        elif tool_name == "fact_feedback":
            return self._handle_fact_feedback(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._config.get("auto_extract", False):
            return
        if not self._store or not messages:
            return
        self._auto_extract_facts(messages)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes as facts.

        Handles all three mutating actions forwarded by the bridge
        (MemoryManager.notify_memory_tool_write):

        - ``add`` → mirror as a new fact.
        - ``replace`` → find the existing fact by ``old_text`` (from metadata)
          and update its content; fall back to ``add`` if not found.
        - ``remove`` → find the existing fact by ``old_text`` and delete it;
          silent no-op if not found (idempotent).

        ``metadata`` carries ``old_text`` for replace/remove, supplied by the
        bridge from the built-in memory tool's ``old_text`` argument.

        Fact lookup uses a direct SQL exact-content match on the ``content``
        column (which has a UNIQUE constraint) rather than FTS5 search. This
        avoids two problems with FTS5 for this maintenance path:

        1. FTS5 tokenizes the query into OR tokens, ranks by relevance, and
           caps results — the exact old fact can be absent from the top-N,
           causing a replace to add a duplicate or a remove to silently no-op.
        2. ``search_facts`` increments ``retrieval_count`` for candidate facts,
           corrupting the retrieval-popularity metric as a side effect of a
           maintenance lookup.
        """
        if not self._store:
            return

        category = "user_pref" if target == "user" else "general"
        old_text = (metadata or {}).get("old_text", "")

        try:
            if action == "add" and content:
                self._store.add_fact(content, category=category)

            elif action == "replace" and content:
                fact_id = self._find_fact_id_by_content(old_text) if old_text else None
                if fact_id is not None:
                    try:
                        self._store.update_fact(fact_id, content=content)
                    except sqlite3.IntegrityError:
                        # The new content already exists as a different fact
                        # (UNIQUE constraint). The replace intent is "old
                        # becomes new" — if new already exists, the old fact is
                        # now redundant. Remove it rather than leaving a stale
                        # duplicate.
                        self._store.remove_fact(fact_id)
                else:
                    # Fact not found in holographic store — mirror as new add
                    # rather than dropping the write silently.
                    self._store.add_fact(content, category=category)

            elif action == "remove":
                if old_text:
                    fact_id = self._find_fact_id_by_content(old_text)
                    if fact_id is not None:
                        self._store.remove_fact(fact_id)
                # No match → idempotent no-op

        except Exception as e:
            logger.warning("Holographic memory_write mirror failed: %s", e, exc_info=True)

    def _find_fact_id_by_content(self, text: str) -> Optional[int]:
        """Find a fact ID by exact content match.

        Uses a direct SQL ``WHERE content = ?`` query on the facts table,
        leveraging the UNIQUE constraint on the ``content`` column. Returns at
        most one row. This avoids the FTS5 tokenization, ranking limits, and
        ``retrieval_count`` side effects of ``search_facts`` — none of which
        belong in a maintenance lookup path.
        """
        if not text or not self._store:
            return None
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT fact_id FROM facts WHERE content = ?", (text.strip(),)
            ).fetchone()
            return int(row["fact_id"]) if row else None

    def shutdown(self) -> None:
        self._store = None
        self._retriever = None

    # -- Tool handlers -------------------------------------------------------

    def _handle_fact_store(self, args: dict) -> str:
        try:
            action = args["action"]
            store = self._store
            retriever = self._retriever

            if action == "add":
                fact_id = store.add_fact(
                    args["content"],
                    category=args.get("category", "general"),
                    tags=args.get("tags", ""),
                )
                return json.dumps({"fact_id": fact_id, "status": "added"})

            elif action == "search":
                results = retriever.search(
                    args["query"],
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", self._min_trust)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "probe":
                results = retriever.probe(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "related":
                results = retriever.related(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "reason":
                entities = args.get("entities", [])
                if not entities:
                    return tool_error("reason requires 'entities' list")
                results = retriever.reason(
                    entities,
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "contradict":
                results = retriever.contradict(
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)})

            elif action == "update":
                updated = store.update_fact(
                    int(args["fact_id"]),
                    content=args.get("content"),
                    trust_delta=float(args["trust_delta"]) if "trust_delta" in args else None,
                    tags=args.get("tags"),
                    category=args.get("category"),
                )
                return json.dumps({"updated": updated})

            elif action == "remove":
                removed = store.remove_fact(int(args["fact_id"]))
                return json.dumps({"removed": removed})

            elif action == "list":
                facts = store.list_facts(
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", 0.0)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"facts": facts, "count": len(facts)})

            else:
                return tool_error(f"Unknown action: {action}")

        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_fact_feedback(self, args: dict) -> str:
        try:
            fact_id = int(args["fact_id"])
            helpful = args["action"] == "helpful"
            result = self._store.record_feedback(fact_id, helpful=helpful)
            return json.dumps(result)
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    # -- Auto-extraction (on_session_end) ------------------------------------

    def _auto_extract_facts(self, messages: list) -> None:
        """Extract structured facts from user messages at session end.

        Instead of dumping raw messages verbatim (the old behavior that caused
        #22907), this extracts the matching portion and wraps it as a clean
        declarative statement. Only extracts from messages that match clear
        preference/decision patterns, and deduplicates against existing facts.
        """
        _PREF_PATTERNS = [
            (re.compile(r'\bI\s+prefer\s+(.+?)(?:\s+(?:over|instead of|rather than)\s+(.+?))?(?:\.|!|$)', re.IGNORECASE), "pref"),
            (re.compile(r'\bI\s+(?:always|never|usually)\s+(.+?)(?:\.|!|$)', re.IGNORECASE), "habit"),
            (re.compile(r'\bmy\s+(?:favorite|preferred|default)\s+(\w+)\s+is\s+(.+?)(?:\.|!|$)', re.IGNORECASE), "pref"),
            (re.compile(r'\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+?)(?:\.|!|$)', re.IGNORECASE), "decision"),
            (re.compile(r'\bthe\s+project\s+(?:uses|needs|requires)\s+(.+?)(?:\.|!|$)', re.IGNORECASE), "project"),
        ]

        extracted = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) < 10:
                continue

            for pattern, kind in _PREF_PATTERNS:
                m = pattern.search(content)
                if not m:
                    continue

                # Build a clean declarative fact from the captured groups
                groups = [g.strip().rstrip('.,;!') for g in m.groups() if g]
                if not groups:
                    continue

                if kind == "pref" and len(groups) >= 2 and " over " in content[m.start():m.end()].lower():
                    fact_text = f"prefers {groups[0]} over {groups[1]}"
                elif kind == "pref":
                    fact_text = f"prefers {groups[0]}"
                elif kind == "habit":
                    habit_word = "always" if "always" in content[m.start():m.end()].lower() else ("never" if "never" in content[m.start():m.end()].lower() else "usually")
                    fact_text = f"{habit_word} {groups[0]}"
                elif kind == "decision":
                    fact_text = f"decided to {groups[0]}"
                elif kind == "project":
                    fact_text = f"project requires {groups[0]}"
                else:
                    fact_text = groups[0]

                # Cap length to avoid storing entire conversations
                if len(fact_text) > 200:
                    fact_text = fact_text[:200]

                category_map = {"pref": "user_pref", "habit": "user_pref", "decision": "project", "project": "project"}
                cat = category_map.get(kind, "general")

                try:
                    self._store.add_fact(fact_text, category=cat)
                    extracted += 1
                except Exception:
                    pass
                break  # one fact per message max

        if extracted:
            logger.info("Auto-extracted %d facts from conversation", extracted)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the holographic memory provider with the plugin system."""
    config = _load_plugin_config()
    provider = HolographicMemoryProvider(config=config)
    ctx.register_memory_provider(provider)