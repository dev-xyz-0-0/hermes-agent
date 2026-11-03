# Phase acceptance checklist

## Phase 1
- [ ] Configurable create_dir resolves correctly
- [ ] Legacy fallback works
- [ ] Discovery/reload works
- [ ] Collisions and unsafe paths rejected
- [ ] Focused and regression tests pass
- [ ] Review approved

## Phase 2
- [ ] Drafts persisted but undiscoverable
- [ ] Validations pass/fail closed
- [ ] No unapproved code execution
- [ ] Content digest invalidation works
- [ ] Focused and regression tests pass
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
