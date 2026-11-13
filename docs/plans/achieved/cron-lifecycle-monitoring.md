# Cron Lifecycle Monitoring — Follow-up Design

## Purpose

Add automated detection for broken cron lifecycle invariants without parsing
rotating text logs or changing execution, delivery, retry, or scheduling
semantics. This is a follow-up design only; the current logging reliability
change does not implement runtime monitoring.

## Durable inputs

- Append lifecycle transitions to profile-scoped
  `cron/lifecycle.jsonl` at the same `tick()` boundary that emits log events.
  Each record contains schema version, event name, timestamp, job ID, run ID,
  profile, and sanitized outcome metadata. It never contains prompts, model
  output, delivery content, credentials, or exception text.
- Atomically update `cron/scheduler-heartbeat.json` after lock acquisition and
  after tick completion with the profile, process ID, timestamp, and state.
- Maintain alert deduplication and acknowledgement state in
  `cron/monitor-state.json`, written atomically under a dedicated monitor lock.
- Apply retention by compacting completed runs only after their configured
  monitoring window. Never depend on `agent.log` or its rotated backups as the
  source of truth.

## Auditor interface

Add `hermes cron audit --json` as a read-only command. It reads the lifecycle
journal, heartbeat, `jobs.json`, and output metadata, then returns:

- exit 0 with `{\"status\": \"ok\", \"violations\": []}` when healthy;
- exit 1 with structured violations when invariants are broken;
- exit 2 when monitoring state is unreadable or internally inconsistent.

Violation types are `execution_terminal_missing`,
`delivery_terminal_missing`, `job_overdue`, `output_without_lifecycle`,
`consecutive_execution_failures`, `consecutive_delivery_failures`, and
`scheduler_heartbeat_stale`. Every violation has a deterministic fingerprint
for deduplication and contains only safe identifiers and timestamps.

Deadlines are derived from existing job timeout, polling interval, and schedule
data, with explicit grace periods configured in a future `cron.monitoring`
section. Until that configuration ships, the command uses documented built-in
defaults and reports them in JSON.

## Alerting and rollout

- Run the auditor from an external service timer or watchdog so scheduler-death
  detection does not depend on the gateway being healthy.
- Let the external monitor translate nonzero exit status or JSON violations
  into its existing alert channel. Do not recursively create Hermes cron jobs
  or use cron delivery for scheduler-health alerts.
- Start in report-only mode, compare naturally scheduled executions with
  `jobs.json` and output timestamps, then enable alerts after a clean observation
  window.
- Deduplicate unresolved fingerprints and emit one recovery notification when
  a violation clears. Never trigger jobs automatically.

## Required tests

- Complete runs, missing terminals, late terminals, delivery failures, overdue
  jobs, unmatched outputs, consecutive failures, and stale heartbeats.
- Duplicate journal records, partial trailing JSONL writes, clock skew, rotated
  operational logs, concurrent audit processes, and profile isolation.
- Redaction and schema tests proving prompts, outputs, environment values, and
  delivery content cannot enter the journal or violation payloads.
- Passive rollout fixtures using temporary `HOME` and `HERMES_HOME`; no live
  provider, delivery adapter, gateway, or production cron execution.
