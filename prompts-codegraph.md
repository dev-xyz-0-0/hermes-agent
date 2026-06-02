For a large repo like Hermes, don't use generic prompts like:

```text
Explain this file.
```

Instead, use graph-oriented prompts that force CodeGraph to leverage relationships.

---

# Architecture Discovery

### Repo Overview

```text
Use codegraph_context first.

Explain the architecture of this repository.

Include:
1. Main entrypoints
2. Core subsystems
3. Dependency flow
4. Important stateful components
5. Critical execution paths
```

---

### Data Flow

```text
Use codegraph_context first.

Trace how data flows from user input to final response.

Show:
- entrypoint
- intermediate transformations
- external services
- output generation
```

---

### Component Responsibilities

```text
Use codegraph_context first.

List the top 20 most important modules and explain their responsibilities.
```

---

# Bug Investigation

### Root Cause Analysis

```text
Use codegraph_context first.

Investigate this bug:

<error>

Identify:
1. Entry point
2. Call chain
3. State mutations
4. Most likely root cause
5. Smallest safe fix
6. Tests to add
```

---

### Exception Tracing

```text
Use codegraph_search and codegraph_trace.

Trace every path that can raise:

<exception>

Show propagation path and handling behavior.
```

---

### Configuration Bugs

```text
Use codegraph_search first.

Trace where this config value is:
1. Defined
2. Loaded
3. Modified
4. Consumed
```

---

# Refactoring

### Safe Refactor

```text
Use codegraph_impact first.

I want to refactor:

<function>

Show:
1. Callers
2. Callees
3. Side effects
4. Potential breakage points
5. Recommended migration strategy
```

---

### Dead Code

```text
Use codegraph_search and codegraph_callers.

Find potentially dead code.

Show:
- functions with no callers
- unused classes
- unreachable paths
```

---

# Testing

### Missing Tests

```text
Use codegraph_context first.

Analyze this module.

Identify:
1. Existing tests
2. Untested paths
3. Edge cases
4. Missing regression tests
```

---

### Regression Test Generator

```text
Use codegraph_context first.

Given this bug:

<description>

Generate:
1. Failing test
2. Root cause
3. Minimal fix
4. Validation plan
```

---

# Hermes-Specific Prompts

### OAuth Flow

```text
Use codegraph_context first.

Trace the complete OpenAI Codex OAuth flow.

Include:
- login initiation
- token storage
- refresh logic
- auth.json updates
- credential pool interactions
```

---

### Credential Pool

```text
Use codegraph_context first.

Explain the credential pool architecture.

Include:
- singleton seeding
- refresh flow
- provider-specific behavior
- synchronization logic
```

---

### Cron Jobs

```text
Use codegraph_context first.

Trace the lifecycle of a cron job.

Show:
1. Creation
2. Persistence
3. Loading
4. Scheduling
5. Execution
6. Failure handling
```

---

### Tool Execution

```text
Use codegraph_context first.

Trace a tool call from model response to tool execution.

Include:
- tool discovery
- registry resolution
- dispatch
- result handling
```

---

# Impact Analysis

### Before Changing Code

```text
Use codegraph_impact first.

If I modify:

<symbol>

What modules, tests, workflows, and user-facing behavior could be affected?
```

---

### Before Merging PR

```text
Use codegraph_impact first.

Review the changes in this branch.

Identify:
1. High-risk modifications
2. Hidden dependencies
3. Missing tests
4. Backward compatibility concerns
```

---

# Learning a New Codebase

My favorite prompt when joining a new project:

```text
Use codegraph_context first.

I am a new engineer on this repository.

Teach me this codebase in the order I should learn it.

For each stage:
1. Files to read
2. Concepts to understand
3. Common pitfalls
4. Related tests
5. Why it matters
```
 