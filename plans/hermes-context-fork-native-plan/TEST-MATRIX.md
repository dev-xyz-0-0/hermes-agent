# Cross-Phase Test Matrix

| Area | Scenario | Expected |
|---|---|---|
| Accounting | W=200k B=100k U=97k | 48.5% window; 97% threshold; 3k headroom |
| Accounting | U > B | Pressure above 100%; zero remaining |
| Accounting | Missing provider usage | Estimate with source or unknown; never fake zero |
| Accounting | Cumulative usage large | Not used as current occupancy |
| Attribution | Built-in + MCP tools | No double counting |
| Attribution | Dynamic memory | Not counted twice |
| Commands | `/context`, `/ctx`, `/context all` | Identical snapshot semantics |
| Commands | Busy turn | Consistent snapshot or explicit stale status |
| Commands | Multiple chats | No cross-session leakage |
| Anchors | Valid prefix + append | Correct delta |
| Anchors | Earlier message rewritten | Anchor rejected |
| Anchors | Model/session changed | Anchor rejected |
| Reset | A at 97%, `/new` to B | No A warning misattributed to B |
| Reset | Other viewer still on A | Its session behavior preserved |
| Reset | Gateway restart | Unknown rather than invented measurement |
| Memory | `/context` repeatedly | No retrieval_count or DB content mutation |
| Security | Prompts contain secrets | No prompt content/secrets in output |
| Reliability | Missing metadata / corrupt anchor | Graceful fallback |
| Reliability | Large skill/MCP inventory | Bounded output |
| Regression | `/new`, `/reset`, `/status` | Existing behavior preserved |

Each test should record its phase, fixture/source, assertions, and executed test command. Prefer deterministic unit tests before gateway E2E tests.
