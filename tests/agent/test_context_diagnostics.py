from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent.context_diagnostics import (
    calculate_context_pressure,
    snapshot_agent_context,
)


def _agent(**compressor_overrides):
    values = {
        "context_length": 200_000,
        "threshold_tokens": 100_000,
        "threshold_percent": 0.5,
        "last_prompt_tokens": 97_000,
        "last_prompt_tokens_source": "provider_usage",
        "last_prompt_tokens_observed_at": 1234.5,
        "compression_count": 2,
    }
    values.update(compressor_overrides)
    return SimpleNamespace(
        model="provider/model",
        session_id="session-a",
        context_compressor=SimpleNamespace(**values),
        _cached_system_prompt="system prompt",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )


def test_pressure_known_example():
    pressure = calculate_context_pressure(97_000, 200_000, 100_000)
    assert pressure.window_percent == 48.5
    assert pressure.compaction_percent == 97.0
    assert pressure.remaining_tokens == 3_000
    assert pressure.over_threshold is False


def test_pressure_above_threshold_is_not_clamped():
    pressure = calculate_context_pressure(125_000, 200_000, 100_000)
    assert pressure.window_percent == 62.5
    assert pressure.compaction_percent == 125.0
    assert pressure.remaining_tokens == 0
    assert pressure.over_threshold is True


@pytest.mark.parametrize(
    ("used", "window", "threshold"),
    [(None, 100, 50), (-1, 100, 50), (10, None, 50), (10, 0, 50), (10, 100, 0)],
)
def test_pressure_invalid_inputs_are_unknown(used, window, threshold):
    pressure = calculate_context_pressure(used, window, threshold)
    assert pressure.window_percent is None
    assert pressure.compaction_percent is None
    assert pressure.remaining_tokens is None
    assert pressure.over_threshold is None


def test_provider_measurement_wins_and_is_session_scoped():
    snapshot = snapshot_agent_context(_agent(), messages=[{"role": "user", "content": "x" * 1000}])
    assert snapshot.measurement.used_tokens == 97_000
    assert snapshot.measurement.provenance == "provider_usage"
    assert snapshot.measurement.observed_at == 1234.5
    assert snapshot.measurement.session_key == "session-a"
    assert snapshot.measurement.model_key == "provider/model"


def test_post_compaction_estimate_keeps_estimate_provenance():
    snapshot = snapshot_agent_context(
        _agent(last_prompt_tokens=30_000, last_prompt_tokens_source="local_estimate")
    )
    assert snapshot.measurement.used_tokens == 30_000
    assert snapshot.measurement.provenance == "local_estimate"


def test_missing_measurement_falls_back_to_full_request_estimate():
    agent = _agent(
        last_prompt_tokens=0,
        last_prompt_tokens_source="unavailable",
        last_prompt_tokens_observed_at=None,
    )
    messages = [{"role": "user", "content": "x" * 400}]
    snapshot = snapshot_agent_context(agent, messages=messages)
    assert snapshot.measurement.used_tokens > 100
    assert snapshot.measurement.provenance == "local_estimate"
    assert snapshot.measurement.observed_at is None


def test_missing_measurement_without_messages_is_unavailable_not_zero():
    snapshot = snapshot_agent_context(
        _agent(last_prompt_tokens=0, last_prompt_tokens_source="unavailable")
    )
    assert snapshot.measurement.used_tokens is None
    assert snapshot.measurement.provenance == "unavailable"
    assert snapshot.pressure.window_percent is None


def test_snapshot_does_not_use_cumulative_session_usage():
    agent = _agent(last_prompt_tokens=0, last_prompt_tokens_source="unavailable")
    agent.session_prompt_tokens = 999_999
    snapshot = snapshot_agent_context(agent)
    assert snapshot.measurement.used_tokens is None


def test_snapshot_is_immutable_and_has_no_side_effects():
    agent = _agent()
    before = dict(vars(agent.context_compressor))
    snapshot = snapshot_agent_context(agent)
    assert vars(agent.context_compressor) == before
    with pytest.raises(FrozenInstanceError):
        snapshot.compression_count = 9


def test_distinct_agents_do_not_share_session_identity():
    first = _agent()
    second = _agent(last_prompt_tokens=12_000)
    second.session_id = "session-b"
    assert snapshot_agent_context(first).measurement.session_key == "session-a"
    assert snapshot_agent_context(second).measurement.session_key == "session-b"
