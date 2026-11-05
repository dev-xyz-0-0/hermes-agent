"""Automatic context window compression for long conversations.

Self-contained class with its own OpenAI client for summarization.
Uses auxiliary model (cheap/fast) to summarize middle turns while
protecting head and tail context.

Improvements over v1:
  - Structured summary template (Goal, Progress, Decisions, Files, Next Steps)
  - Iterative summary updates (preserves info across multiple compactions)
  - Token-budget tail protection instead of fixed message count
  - Tool output pruning before LLM summarization (cheap pre-pass)
  - Scaled summary budget (proportional to compressed content)
  - Richer tool call/result detail in summarizer input
"""

import logging
import threading
import time
from contextvars import copy_context
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from agent.auxiliary_client import (
    AuxiliaryRequestProgress,
    call_llm,
    pin_summary_route,
    resolve_provider_client,
    resolve_task_route,
)
from agent.model_metadata import (
    get_model_context_length,
    estimate_messages_tokens_rough,
)

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier turns in this conversation were compacted "
    "to save context space. The summary below describes work that was "
    "already completed, and the current session state may still reflect "
    "that work (for example, files may already be changed). Use the summary "
    "and the current state to continue from where things left off, and "
    "avoid repeating work:"
)
LEGACY_SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"

# Minimum tokens for the summary output
_MIN_SUMMARY_TOKENS = 2000
# Proportion of compressed content to allocate for summary
_SUMMARY_RATIO = 0.20
# Absolute ceiling for summary tokens (even on very large context windows)
_SUMMARY_TOKENS_CEILING = 12_000

# Placeholder used when pruning old tool results
_PRUNED_TOOL_PLACEHOLDER = "[Old tool output cleared to save context space]"

# Chars per token rough estimate
_CHARS_PER_TOKEN = 4
_SUMMARY_FAILURE_COOLDOWN_SECONDS = 600


class SummaryValidationError(RuntimeError):
    def __init__(self, classification: str):
        super().__init__(classification.replace("_", " "))
        self.classification = classification


def classify_summary_failure(error: Any) -> str:
    """Classify summary failures for diagnostics without changing retry policy."""
    text = str(error or "").lower()
    if not text:
        return "empty_output"
    if any(term in text for term in ("unauthorized", "forbidden", "api key", "authentication", "quota", "billing")):
        return "authentication_or_quota"
    if "context" in text and any(term in text for term in ("length", "window", "overflow", "too long")):
        return "context_overflow"
    if any(term in text for term in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(term in text for term in ("overload", "capacity", "503", "529", "rate limit")):
        return "overload"
    if any(term in text for term in ("network", "connection", "dns", "socket")):
        return "network"
    if any(term in text for term in ("truncated", "max_tokens", "length limit")):
        return "truncated_output"
    return "unknown"


class ContextCompressor:
    """Compresses conversation context when approaching the model's context limit.

    Algorithm:
      1. Prune old tool results (cheap, no LLM call)
      2. Protect head messages (system prompt + first exchange)
      3. Protect tail messages by token budget (most recent ~20K tokens)
      4. Summarize middle turns with structured LLM prompt
      5. On subsequent compactions, iteratively update the previous summary
    """

    def __init__(
        self,
        model: str,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
        summary_target_ratio: float = 0.20,
        quiet_mode: bool = False,
        summary_model_override: str = None,
        base_url: str = "",
        api_key: str = "",
        config_context_length: int | None = None,
        provider: str = "",
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.provider = provider
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_ratio = max(0.10, min(summary_target_ratio, 0.80))
        self.quiet_mode = quiet_mode

        self.context_length = get_model_context_length(
            model, base_url=base_url, api_key=api_key,
            config_context_length=config_context_length,
            provider=provider,
        )
        self.threshold_tokens = int(self.context_length * threshold_percent)
        self.compression_count = 0

        # Derive token budgets: ratio is relative to the threshold, not total context
        target_tokens = int(self.threshold_tokens * self.summary_target_ratio)
        self.tail_token_budget = target_tokens
        self.max_summary_tokens = min(
            int(self.context_length * 0.05), _SUMMARY_TOKENS_CEILING,
        )

        if not quiet_mode:
            logger.info(
                "Context compressor initialized: model=%s context_length=%d "
                "threshold=%d (%.0f%%) target_ratio=%.0f%% tail_budget=%d "
                "provider=%s base_url=%s",
                model, self.context_length, self.threshold_tokens,
                threshold_percent * 100, self.summary_target_ratio * 100,
                self.tail_token_budget,
                provider or "none", base_url or "none",
            )
        self._context_probed = False  # True after a step-down from context error

        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.last_prompt_tokens_source = "unavailable"
        self.last_prompt_tokens_observed_at: float | None = None

        self.summary_model = summary_model_override or ""

        # Stores the previous compaction summary for iterative updates
        self._previous_summary: Optional[str] = None
        self._summary_failure_cooldown_until: float = 0.0
        self.last_summary_failure: Optional[str] = None
        self.last_compaction_outcome: str = "not_attempted"
        self.summary_completion_metadata_available: Optional[bool] = None
        self._attempt_lock = threading.Lock()
        self._attempt_generation = 0
        self.primary_attempt_status = "unavailable"
        self.stall_detection_outcome = "unavailable"
        self.fallback_attempted = False
        self.fallback_provider: Optional[str] = None
        self.fallback_model: Optional[str] = None
        self.logical_attempt_count = 0
        self.commit_fence_state = "unavailable"
        self.compression_cancellation_state = "inactive"
        self.last_fallback_outcome = "unavailable"
        self.transcript_outcome = "unchanged"

    def invalidate_pending_attempts(self) -> None:
        """Prevent in-flight summary results from committing after a reset."""
        if not hasattr(self, "_attempt_lock"):
            self._attempt_lock = threading.Lock()
            self._attempt_generation = 0
        with self._attempt_lock:
            self._attempt_generation += 1

    def _begin_attempt(self) -> int:
        with self._attempt_lock:
            self._attempt_generation += 1
            return self._attempt_generation

    def _attempt_is_current(self, attempt: int) -> bool:
        with self._attempt_lock:
            return attempt == self._attempt_generation

    def _owned_update(self, attempt: int, **values: Any) -> bool:
        """Apply attempt-owned state atomically if *attempt* still owns it."""
        with self._attempt_lock:
            if attempt != self._attempt_generation:
                return False
            for name, value in values.items():
                setattr(self, name, value)
            return True

    def _attempt_update(self, attempt: Optional[int], **values: Any) -> bool:
        """Compatibility update for direct, uncoordinated summary calls."""
        if attempt is not None:
            return self._owned_update(attempt, **values)
        for name, value in values.items():
            setattr(self, name, value)
        return True

    def _begin_logical_attempt(self) -> int:
        with self._attempt_lock:
            self._attempt_generation += 1
            generation = self._attempt_generation
            self.primary_attempt_status = "running"
            self.last_compaction_outcome = "summary_running"
            self.stall_detection_outcome = "pending"
            self.fallback_attempted = False
            self.fallback_provider = None
            self.fallback_model = None
            self.logical_attempt_count = 1
            self.commit_fence_state = "pending"
            self.compression_cancellation_state = "active"
            self.last_fallback_outcome = "not_attempted"
            self.transcript_outcome = "preserved"
            return generation

    def _claim_recovery(self, attempt: int, *, new_generation: bool) -> Optional[int]:
        """Atomically claim the sole recovery permit."""
        with self._attempt_lock:
            if attempt != self._attempt_generation or self.logical_attempt_count >= 2:
                return None
            self.logical_attempt_count += 1
            if new_generation:
                self._attempt_generation += 1
            return self._attempt_generation

    def _revoke_attempt(self, attempt: int) -> Optional[int]:
        """Revoke a stalled worker before releasing its provider request."""
        with self._attempt_lock:
            if attempt != self._attempt_generation:
                return None
            self._attempt_generation += 1
            return self._attempt_generation

    def update_from_response(self, usage: Dict[str, Any]):
        """Update tracked token usage from API response."""
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)
        if self.last_prompt_tokens > 0:
            self.last_prompt_tokens_source = "provider_usage"
            self.last_prompt_tokens_observed_at = time.time()
        else:
            self.last_prompt_tokens_source = "unavailable"
            self.last_prompt_tokens_observed_at = None

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Check if context exceeds the compression threshold."""
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        return tokens >= self.threshold_tokens

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        """Quick pre-flight check using rough estimate (before API call)."""
        rough_estimate = estimate_messages_tokens_rough(messages)
        return rough_estimate >= self.threshold_tokens

    def get_status(self) -> Dict[str, Any]:
        """Get current compression status for display/logging."""
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": min(100, (self.last_prompt_tokens / self.context_length * 100)) if self.context_length else 0,
            "compression_count": self.compression_count,
        }

    # ------------------------------------------------------------------
    # Tool output pruning (cheap pre-pass, no LLM call)
    # ------------------------------------------------------------------

    def _prune_old_tool_results(
        self, messages: List[Dict[str, Any]], protect_tail_count: int,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Replace old tool result contents with a short placeholder.

        Walks backward from the end, protecting the most recent
        ``protect_tail_count`` messages. Older tool results get their
        content replaced with a placeholder string.

        Returns (pruned_messages, pruned_count).
        """
        if not messages:
            return messages, 0

        result = [m.copy() for m in messages]
        pruned = 0
        prune_boundary = len(result) - protect_tail_count

        for i in range(prune_boundary):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not content or content == _PRUNED_TOOL_PLACEHOLDER:
                continue
            # Only prune if the content is substantial (>200 chars)
            if len(content) > 200:
                result[i] = {**msg, "content": _PRUNED_TOOL_PLACEHOLDER}
                pruned += 1

        return result, pruned

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    def _compute_summary_budget(self, turns_to_summarize: List[Dict[str, Any]]) -> int:
        """Scale summary token budget with the amount of content being compressed.

        The maximum scales with the model's context window (5% of context,
        capped at ``_SUMMARY_TOKENS_CEILING``) so large-context models get
        richer summaries instead of being hard-capped at 8K tokens.
        """
        content_tokens = estimate_messages_tokens_rough(turns_to_summarize)
        budget = int(content_tokens * _SUMMARY_RATIO)
        return max(_MIN_SUMMARY_TOKENS, min(budget, self.max_summary_tokens))

    def _serialize_for_summary(self, turns: List[Dict[str, Any]]) -> str:
        """Serialize conversation turns into labeled text for the summarizer.

        Includes tool call arguments and result content (up to 3000 chars
        per message) so the summarizer can preserve specific details like
        file paths, commands, and outputs.
        """
        parts = []
        for msg in turns:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""

            # Tool results: keep more content than before (3000 chars)
            if role == "tool":
                tool_id = msg.get("tool_call_id", "")
                if len(content) > 3000:
                    content = content[:2000] + "\n...[truncated]...\n" + content[-800:]
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            # Assistant messages: include tool call names AND arguments
            if role == "assistant":
                if len(content) > 3000:
                    content = content[:2000] + "\n...[truncated]...\n" + content[-800:]
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc_parts = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            # Truncate long arguments but keep enough for context
                            if len(args) > 500:
                                args = args[:400] + "..."
                            tc_parts.append(f"  {name}({args})")
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_parts.append(f"  {name}(...)")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            # User and other roles
            if len(content) > 3000:
                content = content[:2000] + "\n...[truncated]...\n" + content[-800:]
            parts.append(f"[{role.upper()}]: {content}")

        return "\n\n".join(parts)

    def _generate_summary(
        self, turns_to_summarize: List[Dict[str, Any]], *,
        bypass_cooldown: bool = False, attempt: Optional[int] = None,
        progress: Optional[AuxiliaryRequestProgress] = None,
        defer_failure: bool = False,
    ) -> Optional[str]:
        """Generate a structured summary of conversation turns.

        Uses a structured template (Goal, Progress, Decisions, Files, Next Steps)
        inspired by Pi-mono and OpenCode. When a previous summary exists,
        generates an iterative update instead of summarizing from scratch.

        Returns None if all attempts fail — the caller should drop
        the middle turns without a summary rather than inject a useless
        placeholder.
        """
        now = time.monotonic()
        if not bypass_cooldown and now < self._summary_failure_cooldown_until:
            if defer_failure:
                raise SummaryValidationError("summary_cooldown")
            self._attempt_update(attempt, last_compaction_outcome="summary_cooldown")
            logger.debug(
                "Skipping context summary during cooldown (%.0fs remaining)",
                self._summary_failure_cooldown_until - now,
            )
            return None

        summary_budget = self._compute_summary_budget(turns_to_summarize)
        content_to_summarize = self._serialize_for_summary(turns_to_summarize)

        if self._previous_summary:
            # Iterative update: preserve existing info, add new progress
            prompt = f"""You are updating a context compaction summary. A previous compaction produced the summary below. New conversation turns have occurred since then and need to be incorporated.

PREVIOUS SUMMARY:
{self._previous_summary}

NEW TURNS TO INCORPORATE:
{content_to_summarize}

Update the summary using this exact structure. PRESERVE all existing information that is still relevant. ADD new progress. Move items from "In Progress" to "Done" when completed. Remove information only if it is clearly obsolete.

## Goal
[What the user is trying to accomplish — preserve from previous summary, update if goal evolved]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions — accumulate across compactions]

## Progress
### Done
[Completed work — include specific file paths, commands run, results obtained]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why they were made]

## Relevant Files
[Files read, modified, or created — with brief note on each. Accumulate across compactions.]

## Next Steps
[What needs to happen next to continue the work]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

Target ~{summary_budget} tokens. Be specific — include file paths, command outputs, error messages, and concrete values rather than vague descriptions.

Write only the summary body. Do not include any preamble or prefix."""
        else:
            # First compaction: summarize from scratch
            prompt = f"""Create a structured handoff summary for a later assistant that will continue this conversation after earlier turns are compacted.

TURNS TO SUMMARIZE:
{content_to_summarize}

Use this exact structure:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Progress
### Done
[Completed work — include specific file paths, commands run, results obtained]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why they were made]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Next Steps
[What needs to happen next to continue the work]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

Target ~{summary_budget} tokens. Be specific — include file paths, command outputs, error messages, and concrete values rather than vague descriptions. The goal is to prevent the next assistant from repeating work or losing important details.

Write only the summary body. Do not include any preamble or prefix."""

        call_kwargs = {
            "task": "compression",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": summary_budget * 2,
        }
        if self.summary_model:
            call_kwargs["model"] = self.summary_model
        try:
            response = call_llm(**call_kwargs, progress=progress)
            if attempt is not None and not self._attempt_is_current(attempt):
                return None
            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            response_status = getattr(response, "status", None)
            incomplete = getattr(response, "incomplete_details", None)
            metadata_available = bool(
                finish_reason is not None or response_status is not None or incomplete is not None
            )
            incomplete_reason = (
                incomplete.get("reason") if isinstance(incomplete, dict)
                else getattr(incomplete, "reason", None)
            )
            if (
                str(finish_reason or "").lower() in {
                    "length", "max_tokens", "max_output_tokens", "incomplete", "content_filter",
                }
                or str(response_status or "").lower() == "incomplete"
                or str(incomplete_reason or "").lower() in {"max_tokens", "max_output_tokens"}
            ):
                if defer_failure:
                    raise SummaryValidationError("truncated_output")
                self._attempt_update(
                        attempt,
                        summary_completion_metadata_available=metadata_available,
                        last_summary_failure="truncated_output",
                        last_compaction_outcome="summary_failed",
                        _summary_failure_cooldown_until=time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS,
                    )
                return None
            content = response.choices[0].message.content
            # Handle cases where content is not a string (e.g., dict from llama.cpp)
            if not isinstance(content, str):
                content = str(content) if content else ""
            summary = content.strip()
            if not summary:
                if defer_failure:
                    raise SummaryValidationError("empty_output")
                self._attempt_update(
                        attempt,
                        summary_completion_metadata_available=metadata_available,
                        last_summary_failure="empty_output",
                        last_compaction_outcome="summary_failed",
                        _summary_failure_cooldown_until=time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS,
                    )
                return None
            if not self._attempt_update(
                attempt,
                summary_completion_metadata_available=metadata_available,
                _summary_failure_cooldown_until=0.0,
                last_summary_failure=None,
                last_compaction_outcome="summary_generated",
            ):
                return None
            return self._with_summary_prefix(summary)
        except Exception as e:
            if attempt is not None and not self._attempt_is_current(attempt):
                return None
            if defer_failure:
                raise
            self._attempt_update(
                    attempt,
                    _summary_failure_cooldown_until=time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS,
                    last_summary_failure=classify_summary_failure(e),
                    last_compaction_outcome="summary_failed",
                )
            logging.warning(
                "Failed to generate context summary: %s. "
                "Further summary attempts paused for %d seconds.",
                e,
                _SUMMARY_FAILURE_COOLDOWN_SECONDS,
            )
            return None

    @staticmethod
    def _with_summary_prefix(summary: str) -> str:
        """Normalize summary text to the current compaction handoff format."""
        text = (summary or "").strip()
        for prefix in (LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    # ------------------------------------------------------------------
    # Tool-call / tool-result pair integrity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_tool_call_id(tc) -> str:
        """Extract the call ID from a tool_call entry (dict or SimpleNamespace)."""
        if isinstance(tc, dict):
            return tc.get("id", "")
        return getattr(tc, "id", "") or ""

    def _sanitize_tool_pairs(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fix orphaned tool_call / tool_result pairs after compression.

        Two failure modes:
        1. A tool *result* references a call_id whose assistant tool_call was
           removed (summarized/truncated).  The API rejects this with
           "No tool call found for function call output with call_id ...".
        2. An assistant message has tool_calls whose results were dropped.
           The API rejects this because every tool_call must be followed by
           a tool result with the matching call_id.

        This method removes orphaned results and inserts stub results for
        orphaned calls so the message list is always well-formed.
        """
        surviving_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = self._get_tool_call_id(tc)
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. Remove tool results whose call_id has no matching assistant tool_call
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m for m in messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
            ]
            if not self.quiet_mode:
                logger.info("Compression sanitizer: removed %d orphaned tool result(s)", len(orphaned_results))

        # 2. Add stub results for assistant tool_calls whose results were dropped
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched: List[Dict[str, Any]] = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = self._get_tool_call_id(tc)
                        if cid in missing_results:
                            patched.append({
                                "role": "tool",
                                "content": "[Result from earlier conversation — see context summary above]",
                                "tool_call_id": cid,
                            })
            messages = patched
            if not self.quiet_mode:
                logger.info("Compression sanitizer: added %d stub tool result(s)", len(missing_results))

        return messages

    def _align_boundary_forward(self, messages: List[Dict[str, Any]], idx: int) -> int:
        """Push a compress-start boundary forward past any orphan tool results.

        If ``messages[idx]`` is a tool result, slide forward until we hit a
        non-tool message so we don't start the summarised region mid-group.
        """
        while idx < len(messages) and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    def _align_boundary_backward(self, messages: List[Dict[str, Any]], idx: int) -> int:
        """Pull a compress-end boundary backward to avoid splitting a
        tool_call / result group.

        If the boundary falls in the middle of a tool-result group (i.e.
        there are consecutive tool messages before ``idx``), walk backward
        past all of them to find the parent assistant message.  If found,
        move the boundary before the assistant so the entire
        assistant + tool_results group is included in the summarised region
        rather than being split (which causes silent data loss when
        ``_sanitize_tool_pairs`` removes the orphaned tail results).
        """
        if idx <= 0 or idx >= len(messages):
            return idx
        # Walk backward past consecutive tool results
        check = idx - 1
        while check >= 0 and messages[check].get("role") == "tool":
            check -= 1
        # If we landed on the parent assistant with tool_calls, pull the
        # boundary before it so the whole group gets summarised together.
        if check >= 0 and messages[check].get("role") == "assistant" and messages[check].get("tool_calls"):
            idx = check
        return idx

    # ------------------------------------------------------------------
    # Tail protection by token budget
    # ------------------------------------------------------------------

    def _find_tail_cut_by_tokens(
        self, messages: List[Dict[str, Any]], head_end: int,
        token_budget: int | None = None,
    ) -> int:
        """Walk backward from the end of messages, accumulating tokens until
        the budget is reached. Returns the index where the tail starts.

        ``token_budget`` defaults to ``self.tail_token_budget`` which is
        derived from ``summary_target_ratio * context_length``, so it
        scales automatically with the model's context window.

        Never cuts inside a tool_call/result group. Falls back to the old
        ``protect_last_n`` if the budget would protect fewer messages.
        """
        if token_budget is None:
            token_budget = self.tail_token_budget
        n = len(messages)
        min_tail = self.protect_last_n
        accumulated = 0
        cut_idx = n  # start from beyond the end

        for i in range(n - 1, head_end - 1, -1):
            msg = messages[i]
            content = msg.get("content") or ""
            msg_tokens = len(content) // _CHARS_PER_TOKEN + 10  # +10 for role/metadata
            # Include tool call arguments in estimate
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    msg_tokens += len(args) // _CHARS_PER_TOKEN
            if accumulated + msg_tokens > token_budget and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        # Ensure we protect at least protect_last_n messages
        fallback_cut = n - min_tail
        if cut_idx > fallback_cut:
            cut_idx = fallback_cut

        # If the token budget would protect everything (small conversations),
        # fall back to the fixed protect_last_n approach so compression can
        # still remove middle turns.
        if cut_idx <= head_end:
            cut_idx = fallback_cut

        # Align to avoid splitting tool groups
        cut_idx = self._align_boundary_backward(messages, cut_idx)

        return max(cut_idx, head_end + 1)

    # ------------------------------------------------------------------
    # Main compression entry point
    # ------------------------------------------------------------------

    @staticmethod
    def _control_cancelled(control) -> bool:
        if control is None:
            return False
        try:
            return bool(control.is_cancelled())
        except Exception:
            return True

    @staticmethod
    def _control_publish(control, generation: int, cancel, expected=None):
        if control is None:
            return SimpleNamespace(generation=generation, active=True)
        try:
            return control.publish(generation, cancel, expected=expected)
        except Exception:
            return None

    @staticmethod
    def _control_valid(control, fence, generation: int) -> bool:
        if fence is None:
            return False
        if control is None:
            return bool(getattr(fence, "active", False))
        try:
            return bool(control.valid(fence, generation))
        except Exception:
            return False

    @staticmethod
    def _control_clear(control, fence, generation: int) -> None:
        if fence is None:
            return
        if control is None:
            fence.active = False
            return
        try:
            control.clear(fence, getattr(fence, "generation", generation))
        except Exception:
            pass

    def _run_monitored_summary(
        self, turns: List[Dict[str, Any]], *, generation: int,
        manual: bool, stall_timeout: float, control, fence,
        route: Optional[dict] = None, detect_stall: bool = True,
        progress: Optional[AuxiliaryRequestProgress] = None,
    ) -> SimpleNamespace:
        progress = progress or AuxiliaryRequestProgress()
        result = {"summary": None, "error": None}

        def worker() -> None:
            try:
                if route:
                    with pin_summary_route(route):
                        result["summary"] = self._generate_summary(
                            turns, bypass_cooldown=manual, attempt=generation,
                            progress=progress, defer_failure=True,
                        )
                else:
                    result["summary"] = self._generate_summary(
                        turns, bypass_cooldown=manual, attempt=generation,
                        progress=progress, defer_failure=True,
                    )
            except BaseException as exc:
                result["error"] = exc

        # A copied context prevents a pin or other request-local state from
        # leaking to the detached worker or a concurrent compression.
        context = copy_context()
        thread = threading.Thread(
            target=lambda: context.run(worker), daemon=True,
            name=f"context-summary-{generation}",
        )
        thread.start()
        while thread.is_alive():
            if self._control_cancelled(control) or not self._control_valid(control, fence, generation):
                progress.cancel()
                return SimpleNamespace(summary=None, error=None, stalled=False, cancelled=True)
            if (
                detect_stall
                and progress.supported is True
                and time.monotonic() - progress.last_progress >= max(0.01, stall_timeout)
            ):
                revoked_generation = self._revoke_attempt(generation)
                progress.cancel()
                return SimpleNamespace(
                    summary=None, error=None, stalled=True, cancelled=False,
                    generation=revoked_generation,
                )
            progress.wait(min(0.1, max(0.01, stall_timeout)))
        return SimpleNamespace(
            summary=result["summary"], error=result["error"],
            stalled=False, cancelled=False,
        )

    def _select_fallback_route(self, routes, primary_provider: str, primary_model: str) -> Optional[dict]:
        if isinstance(routes, dict):
            routes = [routes]
        for candidate in routes or ():
            if not isinstance(candidate, dict):
                continue
            provider = str(candidate.get("provider") or "").strip().lower()
            model = str(candidate.get("model") or "").strip()
            if not provider or not model:
                continue
            if provider == str(primary_provider or "").strip().lower() and model == str(primary_model or "").strip():
                continue
            base_url = str(candidate.get("base_url") or "").strip() or None
            api_key = str(candidate.get("api_key") or "").strip() or None
            try:
                client, resolved_model = resolve_provider_client(
                    provider, model=model, explicit_base_url=base_url,
                    explicit_api_key=api_key,
                )
            except Exception:
                continue
            if client is None:
                continue
            return {
                "provider": provider,
                "model": resolved_model or model,
                "base_url": base_url,
                "api_key": api_key,
            }
        return None

    def _finalize_failure(self, generation: int, classification: str, outcome: str) -> None:
        self._owned_update(
            generation,
            last_summary_failure=classification,
            last_compaction_outcome=outcome,
            _summary_failure_cooldown_until=time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS,
            transcript_outcome="preserved",
            compression_cancellation_state="inactive",
        )

    def compress(
        self, messages: List[Dict[str, Any]], current_tokens: int = None, *,
        manual: bool = False, ownership_check=None, fallback_routes=None,
        attempt_control=None, stall_timeout: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """Compress conversation messages by summarizing middle turns.

        Algorithm:
          1. Prune old tool results (cheap pre-pass, no LLM call)
          2. Protect head messages (system prompt + first exchange)
          3. Find tail boundary by token budget (~20K tokens of recent context)
          4. Summarize middle turns with structured LLM prompt
          5. On re-compression, iteratively update the previous summary

        After compression, orphaned tool_call / tool_result pairs are cleaned
        up so the API never receives mismatched IDs.
        """
        original_messages = messages
        attempt = self._begin_logical_attempt()
        n_messages = len(messages)
        if n_messages <= self.protect_first_n + self.protect_last_n + 1:
            if not self.quiet_mode:
                logger.warning(
                    "Cannot compress: only %d messages (need > %d)",
                    n_messages,
                    self.protect_first_n + self.protect_last_n + 1,
                )
            return messages

        display_tokens = current_tokens if current_tokens else self.last_prompt_tokens or estimate_messages_tokens_rough(messages)

        # Phase 1: Prune old tool results (cheap, no LLM call)
        messages, pruned_count = self._prune_old_tool_results(
            messages, protect_tail_count=self.protect_last_n * 3,
        )
        if pruned_count and not self.quiet_mode:
            logger.info("Pre-compression: pruned %d old tool result(s)", pruned_count)

        # Phase 2: Determine boundaries
        compress_start = self.protect_first_n
        compress_start = self._align_boundary_forward(messages, compress_start)

        # Use token-budget tail protection instead of fixed message count
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)

        if compress_start >= compress_end:
            return messages

        turns_to_summarize = messages[compress_start:compress_end]

        if not self.quiet_mode:
            logger.info(
                "Context compression triggered (%d tokens >= %d threshold)",
                display_tokens,
                self.threshold_tokens,
            )
            logger.info(
                "Model context limit: %d tokens (%.0f%% = %d)",
                self.context_length,
                self.threshold_percent * 100,
                self.threshold_tokens,
            )
            tail_msgs = n_messages - compress_end
            logger.info(
                "Summarizing turns %d-%d (%d turns), protecting %d head + %d tail messages",
                compress_start + 1,
                compress_end,
                len(turns_to_summarize),
                compress_start,
                tail_msgs,
            )

        # Phase 3: Generate structured summary under a published commit fence.
        primary_progress = AuxiliaryRequestProgress()
        primary_fence = self._control_publish(
            attempt_control, attempt, primary_progress.cancel,
        )
        if primary_fence is None:
            self._owned_update(
                attempt, last_compaction_outcome="fence_unavailable",
                primary_attempt_status="failed", commit_fence_state="unavailable",
                compression_cancellation_state="inactive",
            )
            return original_messages
        self._owned_update(attempt, commit_fence_state="active")

        # _run_monitored_summary owns its request progress bridge; publish a
        # cancellation closure that is rebound to that bridge by the control.
        run = self._run_monitored_summary(
            turns_to_summarize, generation=attempt, manual=manual,
            stall_timeout=stall_timeout, control=attempt_control,
            fence=primary_fence, progress=primary_progress,
        )
        summary = run.summary
        active_fence = primary_fence
        if not run.stalled:
            self._owned_update(
                attempt,
                stall_detection_outcome=(
                    "no_stall" if primary_progress.supported is True
                    else "unsupported" if primary_progress.supported is False
                    else "not_observed"
                ),
            )

        if run.cancelled:
            self._owned_update(
                attempt, primary_attempt_status="cancelled",
                compression_cancellation_state="cancelled",
                last_compaction_outcome="cancelled", transcript_outcome="preserved",
            )
            self._control_clear(attempt_control, active_fence, attempt)
            return original_messages

        if run.stalled:
            if run.generation is None:
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            attempt = run.generation
            self._owned_update(
                attempt, primary_attempt_status="stalled",
                stall_detection_outcome="confirmed_stall",
                commit_fence_state="revoked",
            )
            if self._control_cancelled(attempt_control):
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            primary_provider, primary_model = resolve_task_route(
                "compression", model=self.summary_model or None,
            )
            route = self._select_fallback_route(
                fallback_routes, primary_provider, primary_model or self.summary_model or self.model,
            )
            if route is None:
                self._finalize_failure(attempt, "timeout", "fallback_unavailable")
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            fallback_generation = self._claim_recovery(attempt, new_generation=False)
            if fallback_generation is None:
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            fallback_progress = AuxiliaryRequestProgress()
            fallback_fence = self._control_publish(
                attempt_control, fallback_generation, fallback_progress.cancel,
                expected=active_fence,
            )
            if fallback_fence is None:
                self._finalize_failure(
                    fallback_generation, "unknown", "fallback_fence_unavailable",
                )
                return original_messages
            attempt = fallback_generation
            active_fence = fallback_fence
            self._owned_update(
                attempt, fallback_attempted=True,
                fallback_provider=route["provider"], fallback_model=route["model"],
                last_fallback_outcome="running", commit_fence_state="active",
            )
            run = self._run_monitored_summary(
                turns_to_summarize, generation=attempt, manual=True,
                stall_timeout=stall_timeout, control=attempt_control,
                fence=active_fence, route=route, detect_stall=True,
                progress=fallback_progress,
            )
            summary = run.summary
            if run.cancelled:
                self._owned_update(
                    attempt, last_fallback_outcome="cancelled",
                    compression_cancellation_state="cancelled",
                    last_compaction_outcome="cancelled",
                )
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            if run.stalled:
                attempt = run.generation or attempt
                self._finalize_failure(attempt, "timeout", "fallback_stalled")
                self._owned_update(attempt, last_fallback_outcome="stalled")
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            if run.error is not None:
                classification = (
                    run.error.classification
                    if isinstance(run.error, SummaryValidationError)
                    else classify_summary_failure(run.error)
                )
                self._finalize_failure(attempt, classification, "fallback_failed")
                self._owned_update(attempt, last_fallback_outcome="failed")
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            self._owned_update(attempt, last_fallback_outcome="succeeded")
        elif run.error is not None:
            classification = (
                run.error.classification
                if isinstance(run.error, SummaryValidationError)
                else classify_summary_failure(run.error)
            )
            if classification == "summary_cooldown":
                self._owned_update(
                    attempt, last_compaction_outcome="summary_cooldown",
                    transcript_outcome="preserved",
                    compression_cancellation_state="inactive",
                )
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            if classification == "context_overflow":
                recovery_generation = self._claim_recovery(attempt, new_generation=False)
                if recovery_generation is not None:
                    self._owned_update(
                        attempt, last_compaction_outcome="overflow_recovery_attempted",
                        primary_attempt_status="overflow_recovery",
                    )
                    recovery_progress = AuxiliaryRequestProgress()
                    recovery_fence = self._control_publish(
                        attempt_control, attempt, recovery_progress.cancel,
                        expected=active_fence,
                    )
                    if recovery_fence is None:
                        self._finalize_failure(attempt, "unknown", "overflow_recovery_failed")
                        return original_messages
                    active_fence = recovery_fence
                    run = self._run_monitored_summary(
                        turns_to_summarize, generation=attempt, manual=True,
                        stall_timeout=stall_timeout, control=attempt_control,
                        fence=active_fence, detect_stall=True,
                        progress=recovery_progress,
                    )
                    summary = run.summary
                    if run.cancelled:
                        self._control_clear(attempt_control, active_fence, attempt)
                        return original_messages
                    if run.stalled:
                        attempt = run.generation or attempt
                        self._finalize_failure(attempt, "timeout", "overflow_recovery_stalled")
                        self._control_clear(attempt_control, active_fence, attempt)
                        return original_messages
                    if run.error is not None:
                        retry_classification = (
                            run.error.classification
                            if isinstance(run.error, SummaryValidationError)
                            else classify_summary_failure(run.error)
                        )
                        self._finalize_failure(attempt, retry_classification, "overflow_recovery_failed")
                        self._control_clear(attempt_control, active_fence, attempt)
                        return original_messages
                else:
                    self._finalize_failure(attempt, classification, "summary_failed")
                    self._control_clear(attempt_control, active_fence, attempt)
                    return original_messages
            else:
                self._finalize_failure(attempt, classification, "summary_failed")
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages

        owns_result = self._attempt_is_current(attempt)
        if ownership_check is not None:
            try:
                owns_result = owns_result and bool(ownership_check())
            except Exception:
                owns_result = False
        owns_result = owns_result and self._control_valid(attempt_control, active_fence, attempt)
        if not summary or not owns_result:
            self._control_clear(attempt_control, active_fence, attempt)
            return original_messages

        # Commit iterative state only after validation and ownership checks.
        summary_body = summary
        if summary_body.startswith(SUMMARY_PREFIX):
            summary_body = summary_body[len(SUMMARY_PREFIX):].lstrip()
        # Phase 4: Assemble compressed message list
        compressed = []
        for i in range(compress_start):
            msg = messages[i].copy()
            if i == 0 and msg.get("role") == "system" and self.compression_count == 0:
                msg["content"] = (
                    (msg.get("content") or "")
                    + "\n\n[Note: Some earlier conversation turns have been compacted into a handoff summary to preserve context space. The current session state may still reflect earlier work, so build on that summary and state rather than re-doing work.]"
                )
            compressed.append(msg)

        _merge_summary_into_tail = False
        if summary:
            last_head_role = messages[compress_start - 1].get("role", "user") if compress_start > 0 else "user"
            first_tail_role = messages[compress_end].get("role", "user") if compress_end < n_messages else "user"
            # Pick a role that avoids consecutive same-role with both neighbors.
            # Priority: avoid colliding with head (already committed), then tail.
            if last_head_role in ("assistant", "tool"):
                summary_role = "user"
            else:
                summary_role = "assistant"
            # If the chosen role collides with the tail AND flipping wouldn't
            # collide with the head, flip it.
            if summary_role == first_tail_role:
                flipped = "assistant" if summary_role == "user" else "user"
                if flipped != last_head_role:
                    summary_role = flipped
                else:
                    # Both roles would create consecutive same-role messages
                    # (e.g. head=assistant, tail=user — neither role works).
                    # Merge the summary into the first tail message instead
                    # of inserting a standalone message that breaks alternation.
                    _merge_summary_into_tail = True
            if not _merge_summary_into_tail:
                compressed.append({"role": summary_role, "content": summary})
        for i in range(compress_end, n_messages):
            msg = messages[i].copy()
            if _merge_summary_into_tail and i == compress_end:
                original = msg.get("content") or ""
                msg["content"] = summary + "\n\n" + original
                _merge_summary_into_tail = False
            compressed.append(msg)

        with self._attempt_lock:
            if attempt != self._attempt_generation:
                self._control_clear(attempt_control, active_fence, attempt)
                return original_messages
            self._previous_summary = summary_body
            self.compression_count += 1
            self.last_compaction_outcome = "compacted"
            self.primary_attempt_status = "succeeded" if not self.fallback_attempted else "stalled"
            self.commit_fence_state = "committed"
            self.compression_cancellation_state = "inactive"
            self.transcript_outcome = "committed"

        self._control_clear(attempt_control, active_fence, attempt)

        compressed = self._sanitize_tool_pairs(compressed)

        if not self.quiet_mode:
            new_estimate = estimate_messages_tokens_rough(compressed)
            saved_estimate = display_tokens - new_estimate
            logger.info(
                "Compressed: %d -> %d messages (~%d tokens saved)",
                n_messages,
                len(compressed),
                saved_estimate,
            )
            logger.info("Compression #%d complete", self.compression_count)

        return compressed
