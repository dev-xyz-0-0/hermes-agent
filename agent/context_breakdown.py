"""Read-only context attribution and bounded text renderers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence

from agent.context_diagnostics import ContextSnapshot, snapshot_agent_context
from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough


_SKILLS_RE = re.compile(
    r"## Skills \(mandatory\).*?If none match, proceed normally without loading a skill\.",
    re.DOTALL,
)
_CONTEXT_RE = re.compile(
    r"# Project Context\n.*?(?=\n\nConversation started:|\Z)",
    re.DOTALL,
)
_SKILL_LINE_RE = re.compile(r"^\s+-\s+([^:\n]+)(?::.*)?$", re.MULTILINE)
_SUBAGENT_TOOLS = frozenset({"delegate_task"})
_CATEGORY_ORDER = (
    ("base_system", "Base / system", "■"),
    ("rules_context", "Rules / context", "▩"),
    ("skills", "Skills", "▤"),
    ("builtin_tools", "Built-in tools", "▣"),
    ("mcp_tools", "MCP tools", "▥"),
    ("subagents", "Subagents", "▦"),
    ("static_memory", "Static memory", "▧"),
    ("dynamic_memory", "Dynamic recall", "◆"),
    ("conversation", "Conversation", "▨"),
    ("unknown", "Unknown / unattributed", "?"),
)
_GLYPHS = {category_id: glyph for category_id, _label, glyph in _CATEGORY_ORDER}


@dataclass(frozen=True)
class ContextCategory:
    id: str
    label: str
    tokens: Optional[int]
    available: bool
    limitation: str = ""
    state: Literal["available", "unavailable", "disabled"] = "available"


@dataclass(frozen=True)
class ContextDetail:
    kind: str
    name: str
    tokens: int
    item_count: int = 1


@dataclass(frozen=True)
class ContextBreakdown:
    snapshot: ContextSnapshot
    categories: tuple[ContextCategory, ...]
    estimated_total: int
    details: tuple[ContextDetail, ...]
    limitations: tuple[str, ...] = ()


def compute_context_breakdown(
    agent: Any,
    *,
    messages: Optional[Sequence[dict[str, Any]]] = None,
    dynamic_memory_context: Optional[str] = None,
    toolset_by_name: Optional[Mapping[str, str]] = None,
) -> ContextBreakdown:
    """Attribute already-materialized provider input without rebuilding it.

    ``messages`` must be the persisted/provider-visible conversation before
    turn-only memory injection. ``dynamic_memory_context`` may contain an
    already-produced recall block; this function never asks a provider for it.
    """
    snapshot = snapshot_agent_context(agent, messages=messages)
    cached_prompt = getattr(agent, "_cached_system_prompt", None)
    prompt_available = isinstance(cached_prompt, str)
    prompt = cached_prompt if prompt_available else ""
    limitations: list[str] = []

    skills_block = _match_text(_SKILLS_RE, prompt) if prompt_available else None
    context_block = _match_text(_CONTEXT_RE, prompt) if prompt_available else None
    static_blocks, static_available, static_state, static_limitation = _static_memory_blocks(agent)
    dynamic_tokens, dynamic_available, dynamic_state, dynamic_limitation = (
        _dynamic_holographic_memory(agent, dynamic_memory_context)
    )

    residual = prompt
    for block in (skills_block, context_block, *static_blocks):
        if block:
            residual = residual.replace(block, "", 1)
    residual = _normalize_separators(residual)

    if prompt_available and not static_available:
        limitations.append(
            "Static memory boundaries were unavailable; any such text remains in base/system."
        )
    limitations.append(
        "Category estimates are heuristic and do not replace authoritative occupancy."
    )

    tools = tuple(getattr(agent, "tools", None) or ())
    builtin_tools, mcp_tools, subagent_tools = _partition_tools(tools)

    values: dict[str, tuple[Any, ...]] = {
        "base_system": _text_value(residual, prompt_available),
        "rules_context": _text_value(context_block or "", prompt_available),
        "skills": _text_value(skills_block or "", prompt_available),
        "builtin_tools": (_schema_tokens(builtin_tools), True, ""),
        "mcp_tools": (_schema_tokens(mcp_tools), True, ""),
        "subagents": (_schema_tokens(subagent_tools), True, ""),
        "static_memory": (
            sum(estimate_tokens_rough(block) for block in static_blocks)
            if static_available else None,
            static_available,
            static_limitation,
            static_state,
        ),
        "dynamic_memory": (
            dynamic_tokens,
            dynamic_available,
            dynamic_limitation,
            dynamic_state,
        ),
        "conversation": (
            estimate_messages_tokens_rough(list(messages)) if messages is not None else None,
            messages is not None,
            "" if messages is not None else "Conversation messages were unavailable.",
        ),
        "unknown": (
            0 if prompt_available else None,
            prompt_available,
            "Unrecognized prompt material is conservatively included in base/system."
            if prompt_available else "Prompt material was unavailable.",
        ),
    }

    categories = tuple(
        ContextCategory(category_id, label, *values[category_id])
        for category_id, label, _glyph in _CATEGORY_ORDER
    )
    estimated_total = sum(category.tokens or 0 for category in categories if category.available)
    details = _build_details(skills_block or "", tools, toolset_by_name or {})
    return ContextBreakdown(
        snapshot=snapshot,
        categories=categories,
        estimated_total=estimated_total,
        details=details,
        limitations=tuple(limitations),
    )


def render_context_breakdown(
    breakdown: ContextBreakdown,
    *,
    grid: bool = False,
    max_chars: int = 4_000,
    max_categories: int = 10,
) -> str:
    """Render a bounded standard view; occupancy and estimates stay separate."""
    lines: list[str] = []
    if grid:
        lines.extend(_render_grid(breakdown))
        lines.append("")
    lines.append("Estimated prompt material by category")
    for category in breakdown.categories[:max(0, max_categories)]:
        glyph = _GLYPHS.get(category.id, "▪")
        if category.state == "disabled":
            lines.append(f"{glyph} {category.label}: disabled")
            continue
        if not category.available or category.tokens is None:
            lines.append(f"{glyph} {category.label}: unavailable")
            continue
        denom = breakdown.snapshot.context_window_tokens or breakdown.estimated_total
        percent = (100.0 * category.tokens / denom) if denom else 0.0
        lines.append(f"{glyph} {category.label}: ~{category.tokens:,} tokens ({percent:.1f}%)")

    lines.extend(["", *_render_occupancy_lines(breakdown)])
    return _bounded(lines, max_chars)


def _render_occupancy_lines(breakdown: ContextBreakdown) -> list[str]:
    snapshot = breakdown.snapshot
    measurement = snapshot.measurement
    lines = ["Authoritative occupancy"]
    if measurement.used_tokens is None:
        lines.append("Used: unavailable")
    else:
        window = f" / {snapshot.context_window_tokens:,}" if snapshot.context_window_tokens else ""
        window_pct = snapshot.pressure.window_percent
        pct = f" ({window_pct:.1f}% of window)" if window_pct is not None else ""
        lines.append(f"Used: {measurement.used_tokens:,}{window} tokens{pct}")
    lines.append(f"Source: {measurement.provenance}")
    if snapshot.pressure.compaction_percent is not None:
        lines.append(
            f"Compaction pressure: {snapshot.pressure.compaction_percent:.1f}% "
            f"({snapshot.pressure.remaining_tokens:,} tokens remaining)"
        )
    return lines


def render_context_details(
    breakdown: ContextBreakdown,
    *,
    max_chars: int = 4_000,
    max_rows: int = 30,
) -> str:
    """Render bounded per-skill and per-toolset estimates without content."""
    lines = [*_render_occupancy_lines(breakdown), "", "Memory attribution"]
    for category_id in ("static_memory", "dynamic_memory"):
        category = next(item for item in breakdown.categories if item.id == category_id)
        if category.state == "disabled":
            lines.append(f"- {category.label}: disabled")
        elif category.available and category.tokens is not None:
            lines.append(f"- {category.label}: ~{category.tokens:,} tokens")
        else:
            lines.append(f"- {category.label}: unavailable")
    lines.extend(["", "Expanded estimates"])
    rows = breakdown.details[:max(0, max_rows)]
    if not rows:
        lines.append("No per-skill or per-toolset details available.")
    for detail in rows:
        name = _safe_name(detail.name, 40)
        count = f", {detail.item_count} tools" if detail.kind == "toolset" else ""
        lines.append(f"- {detail.kind}: {name} — ~{detail.tokens:,} tokens{count}")
    omitted = len(breakdown.details) - len(rows)
    if omitted > 0:
        lines.append(f"… {omitted} more rows omitted")
    return _bounded(lines, max_chars)


def _partition_tools(tools: Sequence[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    builtin: list[dict] = []
    mcp: list[dict] = []
    subagents: list[dict] = []
    for tool in tools:
        name = _tool_name(tool)
        if name.startswith("mcp_"):
            mcp.append(tool)
        elif name in _SUBAGENT_TOOLS:
            subagents.append(tool)
        else:
            builtin.append(tool)
    return builtin, mcp, subagents


def _static_memory_blocks(
    agent: Any,
) -> tuple[tuple[str, ...], bool, Literal["available", "unavailable", "disabled"], str]:
    blocks: list[str] = []
    builtin_available = True
    if not hasattr(agent, "_memory_store"):
        builtin_available = False
    else:
        store = getattr(agent, "_memory_store", None)
        if store is not None:
            snapshot = getattr(store, "_system_prompt_snapshot", None)
            if not isinstance(snapshot, dict):
                builtin_available = False
            else:
                blocks.extend(str(snapshot.get(key) or "") for key in ("memory", "user"))

    has_holographic_attribution = hasattr(agent, "_holographic_static_attribution")
    holographic = _session_owned_attribution(agent, "_holographic_static_attribution")
    holographic_state = _memory_state(holographic) if has_holographic_attribution else "disabled"
    if holographic_state == "available":
        block = getattr(agent, "_holographic_static_memory_snapshot", None)
        if isinstance(block, str) and block:
            blocks.append(block)
        elif _safe_nonnegative_int(holographic.get("tokens")):
            # A persisted prompt can retain the text while its in-memory boundary
            # is unavailable. Do not guess which base/system text was memory.
            holographic_state = "unavailable"

    available = builtin_available and holographic_state != "unavailable"
    if holographic_state == "disabled" and builtin_available:
        state = "available"
    elif not available:
        state = "unavailable"
    else:
        state = "available"
    limitation = "" if available else "Frozen memory snapshot boundaries were unavailable."
    return tuple(block for block in blocks if block), available, state, limitation


def _dynamic_holographic_memory(
    agent: Any,
    supplied: Optional[str],
) -> tuple[Optional[int], bool, Literal["available", "unavailable", "disabled"], str]:
    # Retain the Phase 2 adapter for callers that already hold a produced block.
    if supplied is not None:
        return estimate_tokens_rough(supplied), True, "available", ""
    attribution = _session_owned_attribution(agent, "_holographic_recall_attribution")
    state = _memory_state(attribution)
    if state == "disabled":
        return None, False, "disabled", "Holographic memory is disabled."
    if state != "available":
        return None, False, "unavailable", "No session-owned materialized recall measurement is available."
    tokens = _safe_nonnegative_int(attribution.get("tokens"))
    if tokens is None:
        return None, False, "unavailable", "The materialized recall token estimate is unavailable."
    return tokens, True, "available", ""


def _session_owned_attribution(agent: Any, attribute: str) -> dict[str, Any]:
    value = getattr(agent, attribute, None)
    if not isinstance(value, dict):
        return {}
    session_id = str(getattr(agent, "session_id", "") or "")
    if str(value.get("session_id") or "") != session_id:
        return {}
    return value


def _memory_state(value: Mapping[str, Any]) -> Literal["available", "unavailable", "disabled"]:
    state = str(value.get("state") or "unavailable")
    return state if state in ("available", "unavailable", "disabled") else "unavailable"


def _safe_nonnegative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _build_details(
    skills_block: str,
    tools: Sequence[dict[str, Any]],
    toolset_by_name: Mapping[str, str],
) -> tuple[ContextDetail, ...]:
    details: list[ContextDetail] = []
    for match in _SKILL_LINE_RE.finditer(skills_block):
        name = _safe_name(match.group(1).strip(), 80)
        details.append(ContextDetail("skill", name, estimate_tokens_rough(match.group(0))))

    groups: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        name = _tool_name(tool)
        if name.startswith("mcp_"):
            fallback = "mcp"
        elif name in _SUBAGENT_TOOLS:
            fallback = "subagents"
        else:
            fallback = "built-in"
        groups.setdefault(str(toolset_by_name.get(name) or fallback), []).append(tool)
    for toolset, schemas in sorted(groups.items()):
        details.append(ContextDetail("toolset", toolset, _schema_tokens(schemas), len(schemas)))
    return tuple(details)


def _render_grid(breakdown: ContextBreakdown) -> list[str]:
    window = breakdown.snapshot.context_window_tokens
    cells: list[str] = []
    if window:
        for category in breakdown.categories:
            if category.available and category.tokens:
                count = max(1, round(category.tokens / window * 100))
                cells.extend([_GLYPHS.get(category.id, "▪")] * count)
    cells = cells[:100]
    cells.extend(["·"] * (100 - len(cells)))
    return ["".join(cells[index:index + 20]) for index in range(0, 100, 20)]


def _text_value(text: str, available: bool) -> tuple[Optional[int], bool, str]:
    return (estimate_tokens_rough(text), True, "") if available else (None, False, "Prompt unavailable.")


def _schema_tokens(tools: Sequence[dict[str, Any]]) -> int:
    if not tools:
        return 0
    return estimate_tokens_rough(json.dumps(list(tools), ensure_ascii=False, sort_keys=True, default=str))


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    source = function if isinstance(function, dict) else tool
    return str(source.get("name") or "")


def _match_text(pattern: re.Pattern[str], text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(0) if match else None


def _normalize_separators(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _safe_name(value: str, limit: int) -> str:
    cleaned = " ".join(str(value).split())
    return cleaned if len(cleaned) <= limit else cleaned[: max(0, limit - 1)] + "…"


def _bounded(lines: Sequence[str], max_chars: int) -> str:
    limit = max(80, int(max_chars))
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    marker = "\n… output truncated"
    return text[: max(0, limit - len(marker))].rstrip() + marker
