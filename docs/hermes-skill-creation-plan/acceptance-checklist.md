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
- [ ] Approval bound to exact content/destination
- [ ] Atomic publish and concurrency tested
- [ ] Git stages only intended files
- [ ] Failures accurately reported and recoverable
- [ ] Reload works
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
