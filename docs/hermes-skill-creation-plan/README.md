# Hermes Autonomous Skill Creation — Implementation Pack

Target: https://github.com/dev-xyz-0-0/hermes-agent

This pack contains five sequential implementation phases. Complete and review each phase before advancing. These are plans and implementation-ready prompts, not implemented code.

## Sequence

1. [Phase 1 — Configurable creation directory](phase-01-create-dir.md)
2. [Phase 2 — Draft lifecycle and validation](phase-02-drafts-validation.md)
3. [Phase 3 — Approval, publication and Git](phase-03-publication-git.md)
4. [Phase 4 — Autonomous workflow candidate detection](phase-04-autonomous-detection.md)
5. [Phase 5 — Holographic integration](phase-05-holographic.md)

See [Architecture](architecture.md), [Configuration](configuration.yaml), [Acceptance checklist](acceptance-checklist.md), and [implementation prompt](implementation-prompt.md).

## Ground rules

- Inspect current checked-out repository and AGENTS.md first; paths and APIs may differ.
- Preserve legacy `~/.hermes/skills` behavior when `create_dir` is unset.
- No unapproved execution of generated scripts, publication, commit or push.
- Test with temporary Hermes homes and Git repositories; never destructive-test against production.
- Each phase has a gate and handoff report; stop until the previous phase is accepted.
