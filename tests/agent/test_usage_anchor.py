import json
from types import SimpleNamespace

from agent.context_diagnostics import snapshot_agent_context
from agent.usage_anchor import (
    anchored_context_tokens,
    capture_usage_anchor,
    install_usage_anchor,
    message_fingerprint,
    model_identity,
    restore_usage_anchor,
    validate_usage_anchor,
)
from hermes_state import SessionDB


def _agent(session_id="s1", model="model-a", db=None):
    return SimpleNamespace(
        session_id=session_id, model=model, provider="provider", api_mode="chat",
        base_url="https://example.invalid/v1", _session_db=db,
        _usage_anchor=None, _turn_base_usage_anchor=None,
        context_compressor=SimpleNamespace(
            context_length=100_000, threshold_tokens=80_000, threshold_percent=.8,
            last_prompt_tokens=9_000, last_prompt_tokens_source="provider_usage",
            last_prompt_tokens_observed_at=1.0, compression_count=0,
        ),
        _cached_system_prompt="", tools=[],
    )


def _messages():
    return [
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": {"url": "data:x"}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ]


def test_capture_binds_provider_usage_identity_and_exact_boundary():
    agent = _agent()
    messages = _messages()
    response = {"role": "assistant", "content": "done", "reasoning": "hidden"}
    request = [{"role": "system", "content": "system"}] + messages
    anchor = capture_usage_anchor(
        agent, 1000, 40, messages, response_message=response,
        request_messages=request,
    )
    assert anchor["provenance"] == "provider_usage"
    assert anchor["transcript_length"] == len(messages)
    assert anchor["session_id"] == "s1"
    assert anchor["model_identity"] == model_identity(agent)
    assert anchor["response_fingerprint"] == message_fingerprint(response)
    assert anchor["request_message_count"] == len(request)
    assert "hello" not in json.dumps(anchor)
    assert "result" not in json.dumps(anchor)


def test_valid_append_skips_anchored_response_and_estimates_only_later_delta():
    agent = _agent()
    base = _messages()
    response = {"role": "assistant", "content": "done", "reasoning": "large hidden trace"}
    anchor = capture_usage_anchor(agent, 1000, 40, base, response_message=response)
    exact = anchored_context_tokens(base + [response], anchor, session_id="s1", model_key=model_identity(agent))
    assert exact == (1040, False, anchor["observed_at"])
    appended = base + [response, {"role": "user", "content": "x" * 400}]
    value = anchored_context_tokens(appended, anchor, session_id="s1", model_key=model_identity(agent))
    assert value[0] > 1040
    assert value[1] is True


def test_prefix_rewrite_truncation_session_and_model_changes_fail_closed():
    agent = _agent()
    base = _messages()
    anchor = capture_usage_anchor(agent, 1000, 20, base)
    rewritten = [dict(message) for message in base]
    rewritten[0] = {"role": "user", "content": "changed"}
    assert validate_usage_anchor(anchor, rewritten, session_id="s1", model_key=model_identity(agent)) is None
    assert validate_usage_anchor(anchor, base[:-1], session_id="s1", model_key=model_identity(agent)) is None
    assert validate_usage_anchor(anchor, base, session_id="s2", model_key=model_identity(agent)) is None
    agent.model = "model-b"
    assert validate_usage_anchor(anchor, base, session_id="s1", model_key=model_identity(agent)) is None


def test_anchor_persists_in_existing_metadata_and_restores_after_restart(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("s1", "test", model_config={"max_iterations": 9})
    first = _agent(db=db)
    anchor = capture_usage_anchor(first, 1000, 20, _messages())
    install_usage_anchor(first, anchor)
    assert db.get_session_model_config_value("s1", "max_iterations") == 9
    second = _agent(db=db)
    restore_usage_anchor(second, _messages())
    assert second._usage_anchor == anchor
    db.close()


def test_malformed_metadata_and_reset_are_unavailable(tmp_path):
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("s1", "test", model_config={"_usage_anchor": "bad"})
    agent = _agent(db=db)
    restore_usage_anchor(agent, _messages())
    assert agent._usage_anchor is None
    agent._usage_anchor = {"bad": True}
    agent._turn_base_usage_anchor = {"bad": True}
    # reset_session_state is exercised by integration tests; identity mismatch
    # represents /new and /reset's new session boundary here.
    assert anchored_context_tokens(_messages(), agent._usage_anchor, session_id="new", model_key=model_identity(agent)) is None
    db.close()


def test_snapshot_prefers_turn_base_anchor_and_does_not_mutate_state():
    agent = _agent()
    base = _messages()
    response = {"role": "assistant", "content": "done"}
    anchor = capture_usage_anchor(agent, 1000, 20, base, response_message=response)
    agent._turn_base_usage_anchor = anchor
    before = dict(anchor)
    snapshot = snapshot_agent_context(agent, messages=base + [response])
    assert snapshot.measurement.used_tokens == 1020
    assert snapshot.measurement.provenance == "provider_usage"
    assert anchor == before
