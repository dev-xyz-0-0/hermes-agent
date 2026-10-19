Diagnose this specific Holographic error:

“Holographic memory is unavailable for this session: no chat identity could be resolved to scope memory to (failing closed rather than risk mixing one chat's memory with another's).”

Context:

- Holographic is active and working.
- "fact_store add" works.
- "fact_store search" works across fresh sessions.
- "fact_store list" fails with the error above.
- Do not modify production memory.
- Do not disable the fail-closed behavior.

Task:

1. Locate the exact code path that raises this error.
2. Identify which identity value is missing or unresolved.
3. Trace where that value should come from in the current CLI/session flow.
4. Compare the working "fact_store search" path with the failing "fact_store list" path.
5. Explain why search succeeds but list fails.
6. Determine whether this is:
   - expected behavior,
   - a missing CLI identity propagation issue,
   - inconsistent scope enforcement,
   - or a backport/version mismatch.
7. Check whether an upstream Hermes PR or issue already fixes this exact behavior.
8. Propose the smallest safe fix.

Important constraints:

- Do not make memory global.
- Do not bypass chat/profile isolation.
- Do not hard-code a chat ID.
- Do not edit the SQLite database.
- Prefer fixing identity propagation or local CLI scope resolution.

Return the result in this format:

Root cause:
...

Missing identity:
...

Why search works:
...

Why list fails:
...

Relevant file/function:
...

Relevant upstream PR/issue:
...

Smallest safe fix:
...

Regression tests:

- CLI search works
- CLI list works
- profile isolation remains intact
- network/multi-user sessions still fail closed when identity is genuinely missing


Root cause:

This is a partial-backport/version mismatch that causes missing gateway identity propagation.

Local commit 6d7b9aa added fail-closed, chat-scoped Holographic namespaces. However, it only changed the Holographic provider and tests. It did not update AIAgent or the gateway to pass the authenticated chat identity into MemoryProvider.initialize().

The gateway has source.chat_id, but constructs AIAgent with only user_id. AIAgent then initializes Holographic without chat_id. Because platform="telegram" is multi-tenant, Holographic correctly blocks all operations.

This is not an expected failure for a normal Telegram session. The fail-closed response itself is expected only when identity is genuinely unavailable.

Missing identity:

The missing value is chat_id.

thread_id is also not propagated and should accompany chat_id so Telegram topics and equivalent threaded platforms remain isolated.

Observed flow:

1. gateway/run.py has source.chat_id and source.thread_id.
2. Gateway constructs AIAgent with:
   - platform="telegram"
   - user_id=source.user_id
   - no chat_id
   - no thread_id
3. The local AIAgent constructor only accepts user_id as gateway identity.
4. run_agent.py builds MemoryManager initialization arguments with user_id, but not chat_id/thread_id.
5. Holographic receives:
   - platform="telegram"
   - chat_id=""
6. _resolve_namespace() returns ("", True).
7. handle_tool_call() returns the quoted error before dispatching the requested action.

Gateway logs confirm this repeatedly: Telegram sessions are initialized with “no resolvable chat identity,” even though the gateway’s response logs contain a valid source chat.

Why search works:

Direct fact_store search does not have a separate or weaker authorization path.

Both search and list pass through the same unconditional isolation check in handle_tool_call(). In the same provider instance:

- either both work,
- or both return the fail-closed error.

I reproduced this using temporary databases only:

- CLI provider:
  - add worked
  - search returned one fact
  - list returned the same fact
- Telegram without chat_id:
  - search returned the fail-closed error
  - list returned the identical fail-closed error
- Telegram with chat_id:
  - add, search, and list all worked in namespace telegram:chat-A

Therefore the reported search/add successes did not occur through the same blocked provider instance. They most likely came from one of these paths:

- a real CLI session initialized with platform="cli";
- an older/cached process predating the isolation change;
- a separate session/runtime;
- automatic memory context mistaken for an explicit fact_store search result.

The current live Telegram session now returns the error for search as well as list.

Why list fails:

list is not failing inside MemoryStore.list_facts().

The call is rejected earlier, at HolographicMemoryProvider.handle_tool_call(), because _isolation_blocked is already true. _handle_fact_store() and its list branch are never reached.

The similarly named upstream issue #28055 is not this failure. That issue concerned incorrect SQL LIMIT/OFFSET parameter ordering and returned an empty list, not the chat-identity error. The local list_facts() implementation also does not contain that parameter-ordering defect.

Relevant file/function:

plugins/memory/holographic/__init__.py

- initialize(), lines 661–681:
  - reads chat_id from kwargs;
  - calls _resolve_namespace().
- _resolve_namespace(), lines 699–724:
  - Telegram and other multi-user platforms require chat_id;
  - missing chat_id returns blocked=True.
- handle_tool_call(), lines 1110–1118:
  - raises the exact user-visible error before action dispatch.
- _handle_fact_store(), lines 1299–1307:
  - search implementation.
- _handle_fact_store(), lines 1349–1356:
  - list implementation.

- Both branches are downstream of the same isolation gate.

run_agent.py

- AIAgent.__init__(), lines 433–487:
  - accepts user_id but has no chat_id/thread_id parameters.
- Memory initialization, lines 1042–1059:
  - propagates user_id;
  - does not propagate chat_id or thread_id.

gateway/run.py

- Main gateway AIAgent construction, lines 6901–6922:
  - source.user_id is passed;
  - source.chat_id and source.thread_id are omitted.
- Background gateway agent construction has the same omission.

cli.py

- AIAgent construction, lines 2460–2484:
  - explicitly uses platform="cli".
- CLI is intentionally treated as a single-tenant process and does not require chat_id. I verified that CLI search and list both work.

Classification:

Primary classification: backport/version mismatch.

Concrete manifestation: missing gateway identity propagation.

It is not inconsistent scope enforcement: search and list use the same scope gate.

It is not expected behavior for a legitimate Telegram gateway event, because the gateway already possesses source.chat_id.

Relevant upstream PR/issue:

Exact propagation fix:

PR #13987 — “feat(hindsight): richer session-scoped retain metadata”
https://github.com/NousResearch/hermes-agent/pull/13987

Status: merged on April 22, 2026.

Commit: 8131d7e
https://github.com/NousResearch/hermes-agent/commit/8131d7e

Despite being introduced for Hindsight metadata, the core change is provider-independent. It:

- adds chat_id, thread_id, user_name, chat_name, and chat_type to AIAgent;
- passes source.chat_id and source.thread_id from gateway/run.py;
- forwards those values through MemoryManager into MemoryProvider.initialize().

That is precisely the missing contract Holographic now depends on.

Related broader isolation work:

- Issue #28279 — Per-Chat Memory Isolation:
  https://github.com/NousResearch/hermes-agent/issues/28279
- Issue #34352 — Multi-Tenant Hermes:
  https://github.com/NousResearch/hermes-agent/issues/34352

No upstream issue containing this exact fail-closed error string was found.

Smallest safe fix:

Backport only the identity-propagation portion of upstream PR #13987 into the current architecture:

1. Extend AIAgent.__init__() with:
   - chat_id=None
   - thread_id=None

2. Store them:
   - self._chat_id = chat_id
   - self._thread_id = thread_id

3. Add non-empty values to the MemoryManager initialization arguments:
   - chat_id=self._chat_id
   - thread_id=self._thread_id

4. In every multi-user gateway AIAgent creation path, pass:
   - chat_id=source.chat_id
   - thread_id=source.thread_id

At minimum this includes the primary turn and background-task paths.

5. Restart/evict cached gateway agents after deployment so providers are reinitialized with the corrected identity.

Do not:

- fall back to an empty/global namespace;
- use a hard-coded ID;
- use session_id as a substitute for chat identity;
- disable profile_isolation;
- reinterpret user_id as chat_id;
- modify the database.

The authenticated SessionSource is the correct source of truth.

Regression tests:

- CLI search works
  - Initialize through the real CLI/AIAgent path with platform="cli".
  - Add a fact in a temporary HERMES_HOME.
  - Assert explicit search returns it.

- CLI list works
  - In the same temporary CLI store, assert list returns the added fact.
  - This guards against confusing the unrelated #28055 SQL issue with isolation.

- Gateway identity reaches provider
  - Use a RecordingProvider.
  - Construct a gateway SessionSource with chat_id and thread_id.
  - Assert MemoryProvider.initialize() receives both unchanged.

- Telegram search and list work
  - Initialize through the gateway flow with a real source.chat_id.
  - Assert _isolation_blocked is false.
  - Assert namespace equals telegram:<chat_id> plus thread suffix when present.

- Assert add/search/list all operate within that namespace.

- Profile isolation remains intact
  - Two Telegram chat IDs share one temporary SQLite file.
  - Add different facts in each.
  - Assert search and list from chat A cannot return chat B’s facts, and vice versa.

- Network/multi-user sessions still fail closed
  - Initialize Telegram, Discord, or Slack without chat_id.
  - Assert prefetch returns no content.
  - Assert add, search, and list all return the fail-closed error.
  - Assert no fact is written to the empty namespace.

Verification performed:

- Temporary-database reproduction: CLI and scoped Telegram worked; identity-less Telegram failed closed for both search and list.
- Existing fail-closed/CLI tests: 3 passed.
- Production memory and SQLite database were not modified.