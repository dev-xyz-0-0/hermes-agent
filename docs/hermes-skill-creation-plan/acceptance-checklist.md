# Phase acceptance checklist

## Phase 1
- [x] Configurable create_dir resolves correctly
- [x] Legacy fallback works
- [x] Discovery/reload works
- [x] Collisions and unsafe paths rejected
- [x] Focused and regression tests pass
- [ ] Review approved

## Phase 2
- [x] Drafts persisted but undiscoverable
- [x] Validations pass/fail closed
- [x] No unapproved code execution
- [x] Content digest invalidation works
- [x] Focused and regression tests pass
- [ ] Review approved

## Phase 3
- [x] Approval bound to exact content/destination
- [x] Atomic publish and concurrency tested
- [x] Git stages only intended files
- [x] Failures accurately reported and recoverable
- [x] Reload works
- [ ] Review approved

## Phase 3.1 — Security & reliability hardening
- [x] Approval requires independent human authority (not just a tool call)
- [x] `skills.creation_policy` enforcement (approval_required / legacy)
- [x] Git index contradiction resolved (temp-index commit + narrow real-index sync, with a pre-commit dirty-target-path guard)
- [x] Lock nested-acquisition bug fixed (explicit rejection, not silent reentrancy)
- [x] Test filesystem isolation root-caused and fixed globally (conftest.py)
- [ ] Review approved

## Phase 4
- [ ] Repeated verified success yields candidate
- [ ] Deduplication and cooldown work
- [ ] Suggest-only default
- [ ] No autonomous publication
- [ ] Review approved

## Phase 5
- [ ] Holographic optional
- [ ] Namespace isolation works
- [ ] Memory cannot grant authority
- [ ] Provider outage gracefully handled
- [ ] Review approved

## Required handoff for each phase

- Commit/branch and Git status
- Files changed and why
- Exact test commands and counts
- New config/API behavior
- Security/compatibility risks
- Manual verification steps
- Known issues and next phase prerequisites
