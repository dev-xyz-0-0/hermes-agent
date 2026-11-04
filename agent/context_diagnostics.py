"""Pure, read-only context occupancy and compaction-pressure snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from agent.model_metadata import estimate_request_tokens_rough


MeasurementProvenance = Literal[
    "provider_usage", "anchored_estimate", "local_estimate", "unavailable"
]


@dataclass(frozen=True)
class ContextMeasurement:
    used_tokens: Optional[int]
    provenance: MeasurementProvenance
    observed_at: Optional[float]
    session_key: str
    model_key: str
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPressure:
    window_percent: Optional[float]
    compaction_percent: Optional[float]
    remaining_tokens: Optional[int]
    over_threshold: Optional[bool]


@dataclass(frozen=True)
class ContextSnapshot:
    model_key: str
    session_key: str
    context_window_tokens: Optional[int]
    threshold_tokens: Optional[int]
    threshold_percent: Optional[float]
    measurement: ContextMeasurement
    pressure: ContextPressure
    compression_count: int


def calculate_context_pressure(
    used_tokens: Optional[int],
    context_window_tokens: Optional[int],
    threshold_tokens: Optional[int],
) -> ContextPressure:
    """Calculate window utilization and threshold pressure without clamping."""
    if (
        used_tokens is None
        or used_tokens < 0
        or context_window_tokens is None
        or context_window_tokens <= 0
        or threshold_tokens is None
        or threshold_tokens <= 0
    ):
        return ContextPressure(None, None, None, None)

    return ContextPressure(
        window_percent=100.0 * used_tokens / context_window_tokens,
        compaction_percent=100.0 * used_tokens / threshold_tokens,
        remaining_tokens=max(0, threshold_tokens - used_tokens),
        over_threshold=used_tokens >= threshold_tokens,
    )


def snapshot_agent_context(
    agent: Any,
    *,
    messages: Optional[Sequence[dict[str, Any]]] = None,
) -> ContextSnapshot:
    """Snapshot one live agent without calling providers or mutating agent state.

    A tagged positive compressor reading wins. When it is absent, a local
    estimate is made only when the caller supplies the provider-visible
    conversation messages; the already-cached system prompt and current tool
    schemas are then included. No prompt is rebuilt and no tools are resolved.
    """
    compressor = getattr(agent, "context_compressor", None)
    model_key = str(getattr(agent, "model", "") or "")
    session_key = str(getattr(agent, "session_id", "") or "")

    window = _positive_int(getattr(compressor, "context_length", None))
    threshold = _positive_int(getattr(compressor, "threshold_tokens", None))
    threshold_percent = _positive_float(getattr(compressor, "threshold_percent", None))
    compression_count = max(0, _int_or_zero(getattr(compressor, "compression_count", 0)))

    used = _positive_int(getattr(compressor, "last_prompt_tokens", None))
    source = getattr(compressor, "last_prompt_tokens_source", "unavailable")
    observed_at = getattr(compressor, "last_prompt_tokens_observed_at", None)
    limitations: list[str] = []

    anchored = None
    if messages is not None:
        from agent.usage_anchor import anchored_context_tokens, model_identity
        for candidate in (
            getattr(agent, "_turn_base_usage_anchor", None),
            getattr(agent, "_usage_anchor", None),
        ):
            anchored = anchored_context_tokens(
                list(messages), candidate, session_id=session_key,
                model_key=model_identity(agent),
            )
            if anchored is not None:
                break

    if anchored is not None:
        used, has_estimated_delta, observed_at = anchored
        provenance = "anchored_estimate" if has_estimated_delta else "provider_usage"
        limitations.append(
            "Provider usage is bound to a validated transcript prefix; only appended messages are estimated."
        )
    elif used is not None and source in ("provider_usage", "local_estimate"):
        provenance: MeasurementProvenance = source
        if source == "provider_usage":
            limitations.append("Provider usage describes the most recent completed request.")
        else:
            limitations.append("Post-compaction occupancy is a rough local estimate.")
    elif messages is not None:
        used = estimate_request_tokens_rough(
            list(messages),
            system_prompt=str(getattr(agent, "_cached_system_prompt", "") or ""),
            tools=list(getattr(agent, "tools", None) or []),
        )
        provenance = "local_estimate"
        observed_at = None
        limitations.append("Estimated from cached prompt, supplied messages, and current tool schemas.")
    else:
        used = None
        provenance = "unavailable"
        observed_at = None
        limitations.append("No current prompt measurement or provider-visible messages were available.")

    measurement = ContextMeasurement(
        used_tokens=used,
        provenance=provenance,
        observed_at=observed_at if isinstance(observed_at, (int, float)) else None,
        session_key=session_key,
        model_key=model_key,
        limitations=tuple(limitations),
    )
    return ContextSnapshot(
        model_key=model_key,
        session_key=session_key,
        context_window_tokens=window,
        threshold_tokens=threshold,
        threshold_percent=threshold_percent,
        measurement=measurement,
        pressure=calculate_context_pressure(used, window, threshold),
        compression_count=compression_count,
    )


def render_context_diagnosis(
    agent: Any,
    *,
    messages: Optional[Sequence[dict[str, Any]]] = None,
    active_session_id: Optional[str] = None,
    warning: Optional[dict[str, Any]] = None,
    reset_state: Optional[str] = None,
    turn_active: bool = False,
    max_chars: int = 3_500,
) -> str:
    """Render bounded, read-only lifecycle evidence for reset investigations."""
    snapshot = snapshot_agent_context(agent, messages=messages)
    session_id = str(active_session_id or snapshot.session_key or "")
    short_id = session_id[-12:] if session_id else "unavailable"

    anchor_status = "UNKNOWN — no usage anchor is available"
    if messages is not None:
        from agent.usage_anchor import validate_usage_anchor, model_identity
        candidates = (
            getattr(agent, "_turn_base_usage_anchor", None),
            getattr(agent, "_usage_anchor", None),
        )
        present = [candidate for candidate in candidates if isinstance(candidate, dict)]
        if present:
            valid = any(validate_usage_anchor(
                candidate, list(messages), session_id=session_id,
                model_key=model_identity(agent),
            ) for candidate in present)
            anchor_status = (
                "PASS — anchor belongs to this session and transcript prefix"
                if valid else
                "FAIL — anchor is stale or belongs to another session/model"
            )

    warning_status = "UNKNOWN — no warning metadata is available"
    warning_owner = "unavailable"
    if isinstance(warning, dict):
        warning_owner_id = str(warning.get("session_id") or "")
        warning_owner = warning_owner_id[-12:] if warning_owner_id else "unavailable"
        state = str(warning.get("state") or "unknown")
        if warning_owner_id == session_id:
            warning_status = f"PASS — current-session warning ({state})"
        elif state == "dropped_stale":
            warning_status = "PASS — prior-session warning was rejected"
        else:
            warning_status = f"FAIL — prior-session warning is marked {state}"

    compressor = getattr(agent, "context_compressor", None)
    cooldown_until = float(getattr(compressor, "_summary_failure_cooldown_until", 0.0) or 0.0)
    import time
    cooldown_remaining = max(0, int(cooldown_until - time.monotonic()))
    summary_failure = str(getattr(compressor, "last_summary_failure", "") or "none")
    compaction_outcome = str(getattr(compressor, "last_compaction_outcome", "") or "unavailable")
    completion_metadata = getattr(compressor, "summary_completion_metadata_available", None)
    observed = snapshot.measurement.observed_at
    freshness = f"observed {observed:.3f}" if observed is not None else "timestamp unavailable"
    threshold = snapshot.threshold_tokens

    lines = [
        "Context diagnosis",
        f"Session: …{short_id}",
        f"Active turn: {'yes' if turn_active else 'no'}",
        f"Measurement: {snapshot.measurement.provenance} ({freshness})",
        f"Compaction threshold: {threshold:,}" if threshold else "Compaction threshold: unavailable",
        f"Reset state: {reset_state or 'unknown'}",
        f"Anchor check: {anchor_status}",
        f"Warning owner: …{warning_owner}",
        f"Warning check: {warning_status}",
        f"Last summary failure: {summary_failure}",
        f"Summary cooldown: {'active (' + str(cooldown_remaining) + 's)' if cooldown_remaining else 'inactive'}",
        f"Last compaction outcome: {compaction_outcome}",
        "Summary completion metadata: " + (
            "available" if completion_metadata is True else
            "unavailable; truncation cannot be inferred" if completion_metadata is False else
            "not observed"
        ),
    ]
    return "\n".join(lines)[:max_chars]


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
