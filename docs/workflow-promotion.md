# Workflow Promotion: Exploratory, Routine, and Hybrid Tasks

Charter-worker supports three execution modes for tasks. As you understand a
problem better, you can **promote** a task from exploratory to routine — reducing
cost while preserving reliability.

## The three modes

### Exploratory (`agent: "codex"` or `agent: "claude"`)

The agent gets full LLM reasoning every cycle. It reads `task.md`, uses tools,
browses the web, writes code, and decides what to do.

**Use when:** The task is new, ambiguous, or requires creative problem-solving.
**Cost:** High (full LLM context per cycle).
**Example:** Proactive research, open-ended experiment design.

```yaml
execution:
  agent: "codex"
```

### Routine (`agent: "direct"`)

A fixed script runs every cycle. No LLM is invoked unless the script explicitly
calls one. The orchestrator runs the `entrypoint` command directly.

**Use when:** The task is well-understood and stable. The "what to do" is known;
only the data changes each cycle.
**Cost:** Low (just Python execution; LLM only if the script chooses to call one).
**Example:** Health data parsing, scheduled reports, data aggregation.

```yaml
execution:
  agent: "direct"
  entrypoint: "python run.py"
```

### Hybrid (`agent: "direct"` + self-healing)

A fixed script runs every cycle, but if it crashes, the orchestrator's
self-healing system spawns a diagnostic agent to read logs, identify the root
cause, apply a fix, and retry.

**Use when:** The task is mostly stable but operates in a changing environment
(APIs change, data formats shift, dependencies update).
**Cost:** Low normally; high only on failure (LLM invoked for diagnosis/repair).
**Example:** ETL pipelines, recurring experiments, monitoring tasks.

```yaml
execution:
  agent: "direct"
  entrypoint: "python run.py"

schedule:
  max_runtime_minutes: 30

# Self-healing is automatic — no extra config needed.
# The orchestrator diagnoses and retries on non-zero exit.
```

## How to promote a task

### Step 1: Observe stability

Run the task in exploratory mode for several cycles. Watch the daily emails.
When the agent's actions become repetitive and predictable, it's ready.

### Step 2: Extract the workflow

Look at what the agent actually does each cycle. Write a `run.py` that does
the same thing, calling LLM APIs only where genuine reasoning is needed.

### Step 3: Switch the charter

```yaml
# Before (exploratory)
execution:
  agent: "codex"

# After (routine)
execution:
  agent: "direct"
  entrypoint: "python run.py"
```

### Step 4: Monitor

The task now runs as a cheap direct script. If something breaks, self-healing
kicks in automatically. If the fix requires structural changes, promote back
to exploratory temporarily.

## Why this matters

This is **amortized autonomy**: start with expensive open-ended reasoning,
then crystallize stable patterns into cheap workflows. LLM reasoning is
reserved for genuine uncertainty — search, ambiguity, recovery, and redesign.

The pattern maps directly to organizational theory:
- **Coase**: don't pay agent coordination costs when a simple script suffices
- **Simon**: routinize repeated decisions; use full deliberation only for exceptions
- **March & Simon**: standard operating procedures (SOPs) emerge from experience

See [theory.md](theory.md) for the full motivation.
