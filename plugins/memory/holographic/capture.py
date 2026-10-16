"""Deterministic, rule-based candidate-fact extraction for PR 2 automatic capture.

This is deliberately conservative: it only fires on high-confidence syntactic
patterns (explicit first-person statements of preference/decision/correction/
forgetting), and rejects questions, quotes, and hypotheticals outright. It
does NOT call an LLM — see ``__init__.py``'s ``_capture_turn`` docstring for
why optional LLM-assisted extraction (section 4.5 of the PR 2 spec) is
deferred rather than half-implemented here.

Secret/credential/injection filtering is intentionally NOT duplicated here —
callers must run every candidate's content through the same write firewall
(``_fact_problem`` in ``__init__.py``) used by the explicit ``fact_store``
tool, so there is exactly one place that decides what's safe to store.
"""

from __future__ import annotations

import re
import json
import math
from dataclasses import dataclass
from typing import Any

_MAX_CONTENT_LEN = 200

# Operation markers, checked in this order (most specific first).
_RE_FORGET = re.compile(
    r"^\s*(?:forget|don'?t remember|remove the memory about|stop remembering)\b",
    re.IGNORECASE,
)
_RE_CORRECTION_MARKER = re.compile(
    r"\bswitch(?:ed|ing)?\b|\bactually,?\s+(?:we'?re|i'?m|it'?s)\b|"
    r"\bno\s+longer\s+use\b|\breplace(?:d)?\s+the\s+old\b",
    re.IGNORECASE,
)
_RE_SWITCH_FROM_TO = re.compile(
    r"switch(?:ed|ing)?\s+(?:[\w .]+?\s+)?from\s+(?P<old>[\w .+-]+?)\s+to\s+(?P<new>[\w .+-]+?)[.,!?]?\s*$",
    re.IGNORECASE,
)
_RE_SWITCH_TO = re.compile(
    r"switch(?:ed|ing)?\s+(?:my\s+)?(?P<scope>[\w]+(?:\s+[\w]+){0,3})\s+to\s+(?P<new>[\w .+-]+?)[.,!?]?\s*$",
    re.IGNORECASE,
)
_RE_STATEMENT = re.compile(
    r"\b(?:i\s+use\b|i'?m\s+using\b|i\s+am\s+using\b|i\s+prefer\b|"
    r"my\s+(?:preferred\s+)?\w+(?:\s+\w+)*\s+(?:uses?|is|runs?)\b|"
    r"project\s+\w+\s+uses?\b|"
    r"i'?ve\s+decided\b|from\s+now\s+on\b|we\s+(?:decided|chose)\b|"
    r"i\s+no\s+longer\s+use\b|"
    r"i\s+(?:always|never|usually)\s+\w)",
    re.IGNORECASE,
)

# Reject: questions, hypotheticals, quoted/reported speech, evaluation-not-decision.
_RE_QUESTION = re.compile(r"\?\s*$")
_RE_HYPOTHETICAL = re.compile(
    r"\b(?:what\s+if|suppose|hypothetically|hypothetical\s+scenario|"
    r"imagine(?:\s+that|\s+if)?|assume(?:\s+that)?|pretend(?:\s+that)?|"
    r"for\s+(?:this\s+)?example(?:\s+only)?|fictional|let'?s\s+say|"
    r"evaluating|considering|might\s+(?:use|switch)|"
    r"we'?re\s+(?:evaluating|considering|looking\s+at|exploring))\b",
    re.IGNORECASE,
)
_RE_FULLY_QUOTED = re.compile(r'^\s*["“].*["”]\s*$')
_RE_REPORTED_SPEECH = re.compile(
    r"\b(?:(?:someone|he|she|they|my\s+colleague|a\s+colleague|the\s+article|"
    r"an?\s+article|the\s+docs?|the\s+author)\s+(?:said|says|claims?|prefers?|uses?)|"
    r"according\s+to)\b",
    re.IGNORECASE,
)
_RE_EXPLANATION_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:explain|show|tell|describe|help|walk\s+me\s+through|"
    r"how\s+(?:do|can|should|would)\b|configure\b)", re.IGNORECASE,
)
_RE_PROMPT_INJECTION = re.compile(
    r"\b(?:ignore|disregard|override)\s+(?:all\s+|any\s+)?(?:previous|prior|system|"
    r"original)\s+(?:instructions?|prompts?|rules?)\b|\breveal\s+(?:your\s+)?secrets?\b|"
    r"\bsystem\s+prompt\b", re.IGNORECASE,
)
_RE_ATOMIC_CONTRAST = re.compile(
    r"^\s*i\s+prefer\s+(?P<first>.+?)\s+for\s+my\s+(?P<first_scope>[\w.#+-]+(?:\s+[\w.#+-]+){0,3}\s+projects?),\s*"
    r"but\s+(?P<second>.+?)\s+for\s+my\s+(?P<second_scope>[\w.#+-]+(?:\s+[\w.#+-]+){0,3}\s+projects?)[.!]?\s*$",
    re.IGNORECASE,
)

_ALLOWED_OPERATIONS = frozenset({"statement", "correction", "forget"})
_MODEL_FORBIDDEN_FIELDS = frozenset({
    "namespace", "user_id", "owner", "category", "trust", "trust_score",
    "source", "source_type", "extraction_method", "authorized",
})


@dataclass
class FactCandidate:
    content: str
    operation: str  # "statement" | "correction" | "forget"
    scope: str  # best-effort subject/project hint, for consolidation matching
    old_text: str = ""  # for "correction": the thing being replaced, if named
    confidence: float = 0.6


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def _looks_rejectable(sentence: str) -> bool:
    if _RE_QUESTION.search(sentence):
        return True
    if _RE_HYPOTHETICAL.search(sentence):
        return True
    if _RE_FULLY_QUOTED.match(sentence):
        return True
    if _RE_REPORTED_SPEECH.search(sentence):
        return True
    if _RE_PROMPT_INJECTION.search(sentence):
        return True
    return False


def rejection_reason(text: str, *, for_hybrid: bool = False) -> str | None:
    """Return a stable reason why text is unsafe or too low-signal to capture."""
    normalized = _normalize(text)
    if len(normalized) < 8:
        return "too_short"
    if len(normalized) > 4000:
        return "too_long"
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    for sentence in sentences:
        if _RE_QUESTION.search(sentence):
            return "question"
        if _RE_HYPOTHETICAL.search(sentence):
            return "hypothetical"
        if _RE_FULLY_QUOTED.match(sentence) or _RE_REPORTED_SPEECH.search(sentence):
            return "reported_or_quoted"
        if _RE_PROMPT_INJECTION.search(sentence):
            return "prompt_injection"
        if for_hybrid and _RE_EXPLANATION_REQUEST.search(sentence):
            return "explanatory_request"
    return None


def eligible_for_hybrid(text: str) -> bool:
    """Return whether a user-authored utterance may be sent to the fallback extractor.

    Questions and the same unsafe/low-signal forms rejected by the deterministic
    extractor never reach a model.  This is deliberately stricter than asking the
    model to decide whether a question implies a fact.
    """
    text = _normalize(text)
    if rejection_reason(text, for_hybrid=True):
        return False
    # Explicit memory control remains eligible even though it is imperative.
    if _RE_FORGET.search(text) or _RE_CORRECTION_MARKER.search(text):
        return True
    # Avoid sending arbitrary commands to the auxiliary model. Declarative
    # first-person/project language remains eligible for recall-friendly facts.
    return bool(re.search(
        r"\b(?:i|i'm|i've|my|we|our|project|the\s+.+?\s+(?:is|uses?|runs?|remains?))\b",
        text, re.IGNORECASE,
    ))


def normalize_scope(scope: str, *, content: str = "") -> str:
    """Canonicalize semantic scope without accepting an authorization namespace."""
    value = _normalize(scope).lower().strip(" .:;/-")
    if value in {"", "user", "global", "user-level", "user preference"}:
        return "user"
    if value == "project":
        return "project"
    if value.startswith("project:"):
        value = value.split(":", 1)[1].strip()
    value = re.sub(r"\s+(?:database|db|host|hosting|setup|preference|choice)$", "", value)
    value = re.sub(r"[^a-z0-9_.+# -]", "", value)
    value = _normalize(value)[:80]
    if not value:
        return "user"
    return f"project:{value}"


def candidates_from_json(
    payload: Any, max_candidates: int = 5, *, source_text: str = "",
) -> list[FactCandidate]:
    """Validate untrusted structured model output into bounded candidates."""
    if isinstance(payload, str):
        payload = payload.strip()
        if payload.startswith("```"):
            payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload, flags=re.IGNORECASE)
        payload = json.loads(payload)
    if isinstance(payload, dict):
        payload = payload.get("candidates", [])
    if not isinstance(payload, list):
        raise ValueError("candidate response must be a list")

    candidates: list[FactCandidate] = []
    for raw in payload[:max_candidates]:
        if not isinstance(raw, dict):
            continue
        if _MODEL_FORBIDDEN_FIELDS.intersection(raw):
            continue
        content = _normalize(str(raw.get("content") or ""))
        operation = str(raw.get("operation") or "statement").lower()
        scope_raw = _normalize(str(raw.get("scope") or ""))
        old_text = _normalize(str(raw.get("old_text") or ""))
        if (not content or len(content) > _MAX_CONTENT_LEN or
                len(scope_raw) > 100 or len(old_text) > _MAX_CONTENT_LEN or
                operation not in _ALLOWED_OPERATIONS or _looks_rejectable(content)):
            continue
        try:
            confidence = float(raw.get("confidence", 0.6))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            continue
        if source_text and rejection_reason(source_text, for_hybrid=True):
            continue
        candidates.append(FactCandidate(
            content=content,
            operation=operation,
            scope=normalize_scope(scope_raw, content=content),
            old_text=old_text,
            confidence=confidence,
        ))
    return candidates


def _scope_hint(sentence: str) -> str:
    """Best-effort subject/project token for scope-aware consolidation matching.

    E.g. "My Solana analytics uses PostgreSQL" -> "solana analytics".
    Falls back to "" (global/user-level scope) when nothing distinctive is found.
    """
    m = re.search(r"\bmy\s+([\w]+(?:\s+[\w]+){0,2})\s+(?:uses?|is|runs?)\b", sentence, re.IGNORECASE)
    if m:
        return normalize_scope(m.group(1))
    m = re.search(r"\b(project\s+\w+)\b", sentence, re.IGNORECASE)
    if m:
        return normalize_scope(m.group(1))
    m = re.search(
        r"\bswitch(?:ed|ing)?\s+my\s+([\w]+(?:\s+[\w]+){0,3}?)\s+(?:from|to)\b",
        sentence, re.IGNORECASE,
    )
    if m:
        return normalize_scope(m.group(1))
    m = re.search(r"\bfor\s+my\s+([\w.#+-]+(?:\s+[\w.#+-]+){0,3}\s+projects?)\b", sentence, re.IGNORECASE)
    if m:
        return normalize_scope(m.group(1))
    return "user"


def extract_candidates(text: str, max_candidates: int = 5) -> list[FactCandidate]:
    """Extract candidate facts from one user utterance.

    Deterministic and local — no model calls, no I/O. Splits on sentence-ish
    boundaries so one message can yield a few independent statements, but
    stops at max_candidates. Returns an empty list for chatter, questions,
    hypotheticals, quotes, and anything that doesn't match a known pattern.
    """
    text = _normalize(text)
    if not text or len(text) < 8:
        return []
    if rejection_reason(text):
        return []

    sentences = re.split(r"(?<=[.!?])\s+", text)
    candidates: list[FactCandidate] = []

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or len(sentence) < 8:
            continue
        if _looks_rejectable(sentence):
            continue

        contrast = _RE_ATOMIC_CONTRAST.match(sentence)
        if contrast:
            for value_group, scope_group in (("first", "first_scope"), ("second", "second_scope")):
                value = contrast.group(value_group).strip(" .!?")
                scope_text = contrast.group(scope_group).strip(" .!?")
                candidates.append(FactCandidate(
                    content=f"User prefers {value} for {scope_text}.",
                    operation="statement",
                    scope=normalize_scope(scope_text),
                    confidence=0.8,
                ))
            if len(candidates) >= max_candidates:
                break
            continue

        if _RE_FORGET.search(sentence):
            target = re.sub(_RE_FORGET, "", sentence, count=1).strip(" .!?")
            if target:
                candidates.append(FactCandidate(
                    content=target[:_MAX_CONTENT_LEN],
                    operation="forget",
                    scope=_scope_hint(sentence),
                    confidence=0.8,
                ))
            continue

        switch_ft = _RE_SWITCH_FROM_TO.search(sentence)
        switch_to = None if switch_ft else _RE_SWITCH_TO.search(sentence)
        if switch_ft or switch_to or _RE_CORRECTION_MARKER.search(sentence):
            if switch_ft:
                old = switch_ft.group("old").strip(" .!?")
                new = switch_ft.group("new").strip(" .!?")
                scope = _scope_hint(sentence)
                content = f"{scope or 'current setup'} uses {new}".strip()
                candidates.append(FactCandidate(
                    content=content[:_MAX_CONTENT_LEN],
                    operation="correction",
                    scope=scope,
                    old_text=old,
                    confidence=0.85,
                ))
            elif switch_to:
                new = switch_to.group("new").strip(" .!?")
                scope = normalize_scope(switch_to.group("scope"))
                content = f"{scope.split(':', 1)[-1]} uses {new}".strip()
                candidates.append(FactCandidate(
                    content=content[:_MAX_CONTENT_LEN],
                    operation="correction",
                    scope=scope,
                    confidence=0.8,
                ))
            else:
                # Correction verb present but no clean "from X to Y" — still
                # worth capturing as a statement; consolidation will match it
                # against existing active facts in the same scope.
                candidates.append(FactCandidate(
                    content=sentence[:_MAX_CONTENT_LEN],
                    operation="correction",
                    scope=_scope_hint(sentence),
                    confidence=0.6,
                ))
            continue

        if _RE_STATEMENT.search(sentence):
            candidates.append(FactCandidate(
                content=sentence[:_MAX_CONTENT_LEN],
                operation="statement",
                scope=_scope_hint(sentence),
                confidence=0.7,
            ))

        if len(candidates) >= max_candidates:
            break

    return candidates[:max_candidates]
