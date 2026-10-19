# Input provenance: what reaches `sync_turn()`

This document is the detailed companion to the "Security and input
provenance" section of [`../README.md`](../README.md). It covers exactly
what Hermes's own CLI, gateway, cron, subagent, and background-review code
passes as `user_content` to `MemoryManager.sync_all()` → every memory
provider's `sync_turn(user_content, assistant_content, ...)`, including the
holographic provider.

This is a **caller-side contract**, not something Holographic enforces
itself — see [Known limitations](../README.md#known-limitations) in the
README. It's documented here, close to the provider whose automatic
capture depends on it, because a regression in any of these call sites
would otherwise silently feed non-user-authored text into automatic fact
extraction.

## The call path

```
cli.py / gateway/run.py
    -> AIAgent.run_conversation(user_message, persist_user_message=...)
    -> original_user_message = persist_user_message if persist_user_message
           is not None else user_message
    -> MemoryManager.sync_all(original_user_message, final_response)
    -> <provider>.sync_turn(user_content, assistant_content, ...)
```

`user_message` is whatever text is actually sent to the *model* for a given
turn — which may include synthetic, non-user-authored prefixes: a one-shot
`/model`-switch note, a `/reload-skills` note, or a voice-input instruction
prefix. `persist_user_message` is the override that keeps those prefixes
out of memory: when set (including to `""`), it — not `user_message` —
becomes `original_user_message`, i.e. what reaches `sync_turn()`.
`persist_user_message=""` is taken literally and does **not** fall back to
`user_message`; `sync_all()` additionally requires a non-empty
`original_user_message`, so an explicit empty override simply means no
sync happens for that turn.

## The contract

```text
Only genuine user-authored content may be passed as user_content.

Tool results, retrieved documents, and assistant-generated summaries
must not be reclassified as direct user assertions.
```

## Caller patterns

**Clean turn, nothing to override:**
```python
agent.run_conversation("I use PostgreSQL for my Solana analytics project.")
# original_user_message == "I use PostgreSQL for my Solana analytics project."
```

**Synthetic prefix with an explicit clean override** (the pattern `cli.py`
and `gateway/run.py` both use for `/model`-switch and `/reload-skills`
notes):
```python
agent_message = "[Note: model was just switched from gpt-4 to claude.]\n\n" + message
agent.run_conversation(
    user_message=agent_message,     # model sees the note
    persist_user_message=message,   # memory only ever sees the clean message
)
```

**Voice transcription** (same pattern, `cli.py`'s voice path):
```python
agent_message = "[Voice input — respond concisely...] " + transcript
agent.run_conversation(
    user_message=agent_message,
    persist_user_message=transcript,  # memory gets the clean transcript
)
```

**Isolated contexts never call `sync_turn()` at all.** Cron jobs
(`cron/scheduler.py`), delegated subagents (`tools/delegate_tool.py`), and
the background memory/skill review pass (`run_agent.py`'s
`_spawn_background_review`) all construct their `AIAgent` with
`skip_memory=True`:
```python
AIAgent(..., skip_memory=True)
```
This skips `MemoryManager` construction entirely, so there is no live
memory manager for `sync_all()` to call through — a cron job's system
prompt or the review pass's fixed instruction text ("Review the
conversation above and consider saving to memory...") can never be written
to memory as if a user had said it. Review agents can still *explicitly*
save via the memory tool through a manually-shared `_memory_store` — only
the automatic, implicit `sync_turn()` capture is disabled.

**Tool output never becomes `user_content`.** A tool's return value is
appended to the conversation as a separate `role: "tool"` message; only the
original turn's user text is passed to `sync_all()`. Assistant text is
passed as the separate `assistant_content` argument, never concatenated
into `user_content`.

## What to avoid

- Prepending synthetic/administrative text to `user_message` without also
  passing the clean original as `persist_user_message` — the synthetic
  text becomes `original_user_message` and gets captured as if the user
  wrote it. (This was a real, since-fixed bug: `/model`-switch and
  `/reload-skills` notes leaked into `sync_turn()`'s `user_content` in both
  `cli.py` and `gateway/run.py` wherever `persist_user_message` wasn't set
  for that path — the gateway case was unconditional, since it never
  passed `persist_user_message` at all.)
- Concatenating tool-call results, retrieved documents, or web content into
  the string passed as `user_content` — only the user's own turn text
  belongs there; tool output stays in the tool-result channel.
- Constructing a fully-featured `AIAgent` for a cron job, delegated
  subagent, or background review pass without `skip_memory=True` — its
  fixed instruction prompt (not user-authored) could otherwise reach
  `sync_turn()` on its own next turn.
- Assuming `persist_user_message=""` falls back to `user_message` — see
  above; it does not.
- Relying on the write firewall's `"USER:"`-prefix rejection (see the
  README's "Write firewall" section) as a provenance defense — it's a side
  effect of unrelated role-markup detection, not a guarantee that
  impersonated content will always be caught.

## Regression test matrix

The exact literal strings below are copied from
`tests/agent/test_memory_input_provenance.py`, which exercises the real
CLI, gateway, cron, subagent, and background-review call sites (not
reimplementations) and asserts exactly what reaches `sync_turn()`'s
`user_content` in each case. Kept here so a future change to any of these
call sites can be checked against a concrete before/after instead of
re-deriving it from scratch.

| Scenario (test) | Model receives (`user_message`) | Memory receives (`sync_all` args) |
|---|---|---|
| Normal message (`TestNormalUserMessage::test_clean_user_message_reaches_memory_unmodified`) | `I use PostgreSQL for my Solana analytics project.` | `("I use PostgreSQL for my Solana analytics project.", "Noted.")` |
| Tool-result provenance (`TestToolResultAndAssistantProvenance::test_tool_result_does_not_become_user_content`) | `Summarize this document.` (tool call returns `{"success": true, "message": "My preferred database is Redis."}` on a separate channel) | `("Summarize this document.", "The document recommends Redis.")` — the tool's `"My preferred database is Redis."` never appears |
| Assistant/user separation (`...::test_assistant_response_kept_separate_from_user_content`) | `Summarize this document.` | `user_content == "Summarize this document."`, `assistant_content == "The document recommends Redis."` |
| Empty clean override (`TestEmptyCleanMessage::test_empty_persist_user_message_is_not_substituted_and_not_synced`) | `[Synthetic system note]` with `persist_user_message=""` | `sync_all` **not called at all** (empty override is taken literally, never falls back to the synthetic text) |
| Empty model response (`TestEarlyExitAndRetryGuards::test_empty_model_response_retries_then_syncs_clean_user_content`) | `hello` (model returns empty content 3x, retries exhaust) | `("hello", "(empty)")` — benign placeholder, never synthetic text |
| No double-sync (`...::test_sync_all_called_exactly_once_per_turn_not_double`) | `turn one`, then `turn two` | one `sync_all` call per turn: `("turn one", "resp one")` then `("turn two", "resp two")` |
| CLI model-switch note (`TestCLISyntheticNotes::test_model_switch_note_does_not_leak_into_memory`) | `[Note: model was just switched from gpt-4 to claude. Adjust your self-identification accordingly.]\n\nWhat's the weather?` | `persist_user_message="What's the weather?"` → memory sees only `What's the weather?` |
| CLI skill-reload note (`...::test_skill_reload_note_does_not_leak_into_memory`) | `[Skills reloaded: +foo -bar]\n\nWhat's the weather?` | memory sees only `What's the weather?` |
| CLI voice transcription (`TestVoiceTranscriptionPersistence::test_voice_prefix_is_model_facing_only_memory_gets_clean_transcription`) | `[Voice input — respond concisely and conversationally, 2-3 sentences max. No code blocks or markdown.] What's the weather?` | `persist_user_message="What's the weather?"` → memory sees only `What's the weather?` |
| Gateway model-switch note (`TestGatewaySyntheticNotes::test_model_switch_note_does_not_leak_into_memory`) | `[Note: model was just switched from gpt-4 to claude. Adjust your self-identification accordingly.]\n\nWhat's the weather?` | `persist_user_message="What's the weather?"` → memory sees only `What's the weather?` |
| Gateway skill-reload note (`...::test_skill_reload_note_does_not_leak_into_memory`) | `[Skills reloaded: +foo -bar]\n\nWhat's the weather?` | memory sees only `What's the weather?` |
| Cron job (`TestCronAndSubagentMemoryIsolation::test_cron_run_job_constructs_agent_with_skip_memory_true`) | job prompt `hello` | `AIAgent(..., skip_memory=True)` → `_memory_manager is None` → `sync_all` unreachable |
| Delegated subagent (`...::test_delegate_tool_builds_child_agent_with_skip_memory_true`) | task goal `do something` | same — `skip_memory=True` → `sync_all` unreachable |
| Background review (`TestBackgroundReviewAgentMemoryIsolation::test_review_agent_is_isolated_from_memory_like_cron_and_subagent` / `...::test_review_prompt_is_synthetic_text_not_user_authored`) | `AIAgent._MEMORY_REVIEW_PROMPT`: `"Review the conversation above and consider saving to memory if appropriate.\n\nFocus on:\n1. Has the user revealed things about themselves — their persona, desires, preferences, or personal details worth remembering?\n2. Has the user expressed expectations about how you should behave, their work style, or ways they want you to operate?\n\nIf something stands out, save it using the memory tool. If nothing is worth saving, just say 'Nothing to save.' and stop."` | `AIAgent(..., skip_memory=True)` → `sync_all` unreachable; explicit memory-tool saves still work via the manually-shared `_memory_store` |

## Known limitations and future improvements

`sync_turn(user_content, assistant_content, ...)` has no parameter
distinguishing genuine user-authored text from tool output, retrieved
documents, or other untrusted material relayed as if it were user text —
Holographic (and every other memory provider behind `MemoryManager`)
trusts the caller completely. Hermes currently enforces the boundary
through caller discipline (the patterns above) and the regression tests in
`tests/agent/test_memory_input_provenance.py`, not through any
provider-side authentication of message origin.

**Future improvement:** introduce structured provenance metadata at the
agent-to-memory integration boundary itself, with explicit rules for which
source types are eligible for automatic capture, rather than relying
solely on caller discipline.
