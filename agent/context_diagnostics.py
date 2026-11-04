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

    if used is not None and source in ("provider_usage", "local_estimate"):
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
