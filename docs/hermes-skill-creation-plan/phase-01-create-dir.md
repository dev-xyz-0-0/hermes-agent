# Phase 1 — Configurable skill creation directory

## Goal

`skills.create_dir: /home/ubuntu/skills-pack/skills` makes `skill_manage(create)` create new skills in that directory, while legacy installs continue to use `~/.hermes/skills`.

## Implementation tasks

1. Inspect config loader/schema, skill manager, discovery, reload, profile handling and tests. Check whether an upstream implementation already exists.
2. Introduce a central resolver (suggested `agent/skill_paths.py`) with `get_skill_create_dir`, `get_skill_discovery_dirs`, `resolve_new_skill_path`, and destination validation. Reuse existing conventions and avoid stale import-time constants.
3. Parse `skills.create_dir`; accept an absolute path or documented home expansion; reject malformed values and unsafe paths; preserve default.
4. Include `create_dir` in discovery automatically, deduplicated against `external_dirs` and default directory. Define and test precedence for duplicate skill names.
5. Update `skill_manage(create)` to resolve dynamically. Reject collisions across discoverable skills; validate categories, names, symlinks and permissions.
6. Do not grant edit/delete rights merely because a directory appears in `external_dirs`.
7. Ensure CLI and gateway reload/lookup discover newly created skills.
8. Update docs, sample config, and changelog as appropriate.

## Tests

- No config -> default location.
- Configured path -> correct nested category/name.
- Profile and HERMES_HOME overrides.
- Duplicate external/create paths deduplicated.
- Name collision with legacy/local/external paths rejected.
- Read-only destination fails without partial files.
- Path traversal and symlink escape rejected.
- Config reload does not use stale path.
- Skill list/view/reload resolve newly created skill.

## Gate

With temporary directories, create `crypto/test-skill` through existing tool and prove it appears only in custom destination, is discoverable and can be reloaded. Existing default behavior and related regressions pass.

## Handoff

Document changed files, config snippet, exact test commands and results, lookup precedence, compatibility risks and commit hash (if approved).
