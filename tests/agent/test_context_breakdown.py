from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.context_breakdown import (
    compute_context_breakdown,
    render_context_breakdown,
    render_context_details,
)
from agent.model_metadata import estimate_tokens_rough


def _tool(name: str, description: str = "tool") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _agent():
    memory = "# Memory\n- likes concise output"
    user = "# User\n- developer"
    skills = (
        "## Skills (mandatory)\nBefore replying, scan the skills below.\n"
        "<available_skills>\n  coding:\n    - python-review: Review Python\n"
        "</available_skills>\nIf none match, proceed normally without loading a skill."
    )
    rules = (
        "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n"
        "## AGENTS.md\n\nUse tests."
    )
    prompt = "\n\n".join([
        "You are Hermes.", memory, user, skills, rules,
        "Conversation started: Monday\nModel: provider/model",
    ])
    compressor = SimpleNamespace(
        context_length=200_000,
        threshold_tokens=100_000,
        threshold_percent=0.5,
        last_prompt_tokens=97_000,
        last_prompt_tokens_source="provider_usage",
        last_prompt_tokens_observed_at=123.0,
        compression_count=0,
    )
    return SimpleNamespace(
        model="provider/model",
        session_id="session-a",
        context_compressor=compressor,
        _cached_system_prompt=prompt,
        _memory_store=SimpleNamespace(
            _system_prompt_snapshot={"memory": memory, "user": user}
        ),
        tools=[
            _tool("read_file", "built in"),
            _tool("mcp_github_read_file", "mcp"),
            _tool("delegate_task", "subagent"),
        ],
    )


def _categories(breakdown):
    return {category.id: category for category in breakdown.categories}


def test_categories_are_mutually_exclusive_and_keep_occupancy_separate():
    breakdown = compute_context_breakdown(
        _agent(),
        messages=[{"role": "user", "content": "hello"}],
        dynamic_memory_context="<memory-context>recalled fact</memory-context>",
    )
    categories = _categories(breakdown)
    assert categories["base_system"].tokens > 0
    assert categories["rules_context"].tokens > 0
    assert categories["skills"].tokens > 0
    assert categories["static_memory"].tokens > 0
    assert categories["dynamic_memory"].tokens > 0
    assert categories["conversation"].tokens > 0
    assert categories["builtin_tools"].tokens > 0
    assert categories["mcp_tools"].tokens > 0
    assert categories["subagents"].tokens > 0
    assert breakdown.snapshot.measurement.used_tokens == 97_000
    assert breakdown.estimated_total != breakdown.snapshot.measurement.used_tokens


def test_each_tool_schema_is_counted_in_exactly_one_category():
    breakdown = compute_context_breakdown(_agent(), messages=[])
    categories = _categories(breakdown)
    assert all(categories[name].tokens > 0 for name in ("builtin_tools", "mcp_tools", "subagents"))
    detail_count = sum(detail.item_count for detail in breakdown.details if detail.kind == "toolset")
    assert detail_count == 3


def test_skills_removed_from_base_and_expanded_without_content_disclosure():
    breakdown = compute_context_breakdown(_agent(), messages=[])
    skill_details = [detail for detail in breakdown.details if detail.kind == "skill"]
    assert [detail.name for detail in skill_details] == ["python-review"]
    assert "Review Python" not in render_context_details(breakdown)


def test_unavailable_partial_categories_are_explicit():
    agent = _agent()
    del agent._cached_system_prompt
    del agent._memory_store
    breakdown = compute_context_breakdown(agent)
    categories = _categories(breakdown)
    assert categories["base_system"].available is False
    assert categories["static_memory"].available is False
    assert categories["dynamic_memory"].available is False
    assert categories["conversation"].available is False
    rendered = render_context_breakdown(breakdown)
    assert "Base / system: unavailable" in rendered
    assert "Dynamic recall: unavailable" in rendered


def test_empty_configured_categories_are_zero_not_unavailable():
    agent = _agent()
    agent._cached_system_prompt = "base only"
    agent._memory_store = None
    agent.tools = []
    breakdown = compute_context_breakdown(agent, messages=[], dynamic_memory_context="")
    categories = _categories(breakdown)
    for name in ("rules_context", "skills", "builtin_tools", "mcp_tools", "subagents",
                 "static_memory", "dynamic_memory", "conversation"):
        assert categories[name].available is True
        assert categories[name].tokens == 0


def test_expanded_toolsets_use_supplied_fork_metadata():
    breakdown = compute_context_breakdown(
        _agent(),
        messages=[],
        toolset_by_name={
            "read_file": "file_tools",
            "mcp_github_read_file": "mcp-github",
            "delegate_task": "delegation",
        },
    )
    names = {detail.name for detail in breakdown.details if detail.kind == "toolset"}
    assert names == {"file_tools", "mcp-github", "delegation"}


def test_renderers_are_bounded_and_pressure_is_not_clamped():
    agent = _agent()
    agent.context_compressor.last_prompt_tokens = 125_000
    breakdown = compute_context_breakdown(agent, messages=[])
    standard = render_context_breakdown(breakdown, grid=True, max_chars=1_000)
    expanded = render_context_details(breakdown, max_chars=500, max_rows=2)
    assert len(standard) <= 1_000
    assert len(expanded) <= 500
    assert "Compaction pressure: 125.0%" in standard
    assert "more rows omitted" in expanded


def test_read_only_does_not_call_prompt_tools_or_memory_methods():
    agent = _agent()
    before_prompt = agent._cached_system_prompt
    before_tools = list(agent.tools)
    before_compressor = dict(vars(agent.context_compressor))
    breakdown = compute_context_breakdown(agent, messages=[])
    assert breakdown.categories
    assert agent._cached_system_prompt == before_prompt
    assert agent.tools == before_tools
    assert vars(agent.context_compressor) == before_compressor


def test_unicode_and_long_names_render_safely():
    agent = _agent()
    long_name = "工具" * 50
    agent.tools.append(_tool(long_name))
    breakdown = compute_context_breakdown(
        agent, messages=[], toolset_by_name={long_name: long_name}
    )
    rendered = render_context_details(breakdown, max_chars=1_000)
    assert "工具" in rendered
    assert len(rendered) <= 1_000


def test_holographic_static_and_materialized_recall_are_attributed_without_double_counting():
    agent = _agent()
    static_block = "# Holographic Memory\nActive recall policy."
    recall_block = "<memory-context>\nprivate recalled fact\n</memory-context>"
    agent._cached_system_prompt = agent._cached_system_prompt.replace(
        "You are Hermes.", f"You are Hermes.\n\n{static_block}"
    )
    agent._holographic_static_memory_snapshot = static_block
    agent._holographic_static_attribution = {
        "state": "available", "session_id": "session-a",
        "tokens": estimate_tokens_rough(static_block),
        "provenance": "frozen_system_prompt_block",
    }
    agent._holographic_recall_attribution = {
        "state": "available", "session_id": "session-a",
        "tokens": estimate_tokens_rough(recall_block),
        "provenance": "injected_fenced_block",
    }

    baseline = compute_context_breakdown(_agent(), messages=[])
    breakdown = compute_context_breakdown(agent, messages=[])
    categories = _categories(breakdown)

    assert categories["static_memory"].tokens == (
        _categories(baseline)["static_memory"].tokens + estimate_tokens_rough(static_block)
    )
    assert categories["base_system"].tokens == _categories(baseline)["base_system"].tokens
    assert categories["dynamic_memory"].tokens == estimate_tokens_rough(recall_block)
    rendered = render_context_breakdown(breakdown) + render_context_details(breakdown)
    assert "private recalled fact" not in rendered


def test_holographic_disabled_no_recall_and_stale_session_states_are_explicit():
    agent = _agent()
    agent._holographic_static_attribution = {
        "state": "disabled", "session_id": "session-a", "tokens": None,
    }
    agent._holographic_recall_attribution = {
        "state": "disabled", "session_id": "session-a", "tokens": None,
    }
    disabled = compute_context_breakdown(agent, messages=[])
    assert _categories(disabled)["dynamic_memory"].state == "disabled"
    assert "Dynamic recall: disabled" in render_context_breakdown(disabled)

    agent._holographic_recall_attribution = {
        "state": "available", "session_id": "session-a", "tokens": 0,
        "provenance": "no_recall",
    }
    no_recall = compute_context_breakdown(agent, messages=[])
    assert _categories(no_recall)["dynamic_memory"].tokens == 0

    agent._holographic_recall_attribution["session_id"] = "another-session"
    stale = compute_context_breakdown(agent, messages=[])
    assert _categories(stale)["dynamic_memory"].state == "unavailable"


def test_holographic_diagnostics_do_not_call_memory_provider_or_database():
    agent = _agent()
    agent._memory_manager = MagicMock()
    agent._holographic_static_attribution = {
        "state": "unavailable", "session_id": "session-a", "tokens": None,
    }
    agent._holographic_recall_attribution = {
        "state": "available", "session_id": "session-a", "tokens": 5,
        "provenance": "injected_fenced_block",
    }

    compute_context_breakdown(agent, messages=[])

    agent._memory_manager.prefetch_all.assert_not_called()
    agent._memory_manager.get_provider.assert_not_called()
    agent._memory_manager.initialize_all.assert_not_called()
