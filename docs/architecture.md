# Architecture

## System Overview

```
                          ┌─────────────────────────────────────┐
                          │         CRON (every hour)           │
                          └──────────────┬──────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────────┐
                    │              ORCHESTRATOR                    │
                    │  schedule check · preflight · lock · spawn  │
                    └─┬────────┬────────┬────────┬───────────┬───┘
                      │        │        │        │           │
               ┌──────▼──┐ ┌──▼─────┐ ┌▼──────┐ ┌▼────────┐ ┌▼──────────┐
               │ Daily    │ │ Paper  │ │Health │ │Job      │ │ Research  │
               │ Planner  │ │ Reader │ │Asst.  │ │Search   │ │ (LTT)    │
               │          │ │        │ │       │ │         │ │          │
               │ agent    │ │ direct │ │ agent │ │ direct  │ │ direct   │
               │ (codex)  │ │ + LLM  │ │(claude│ │ + LLM   │ │ + LLM    │
               └──────┬───┘ └───┬────┘ └──┬────┘ └────┬────┘ └────┬─────┘
                      │         │         │           │            │
                      ▼         ▼         ▼           ▼            ▼
               ┌─────────────────────────────────────────────────────────┐
               │                    DAILY SUMMARIES                      │
               │          summary.md + summary.json per task             │
               └──────────────────────┬──────────────────────────────────┘
                                      │
                         ┌────────────▼─────────────┐
                         │      DAILY DIGEST        │
                         │  health report + results  │──────▶  EMAIL TO USER
                         └────────────┬─────────────┘         (user reads,
                                      │                        optionally
           ┌──────────────────────────┘                        replies)
           │
           │  ◄── on task crash/timeout
           │
    ┌──────▼───────────────────────────────────────────────────┐
    │                SELF-HEALING LAYER                         │
    │                                                          │
    │  REACTIVE (per-crash)          PROACTIVE (daily 4 AM)    │
    │  ┌─────────────────────┐      ┌────────────────────────┐ │
    │  │ Diagnose crash      │      │ Analyze 7-day trends   │ │
    │  │ Read logs + code    │      │ Detect failure patterns │ │
    │  │ Apply code/cfg fix  │      │ Apply durable fixes    │ │
    │  │ Retry (up to 3x)   │      │ Smoke test fixes       │ │
    │  └────────┬────────────┘      │ Assess engagement      │ │
    │           │                   │ Rate task value         │ │
    │           │                   └───────────┬────────────┘ │
    └───────────┼───────────────────────────────┼──────────────┘
                │                               │
                └──────────┐  ┌─────────────────┘
                           │  │
                           ▼  ▼
                  ┌────────────────────┐
                  │  fixes applied,    │
                  │  state updated,    │──────▶  NEXT HOURLY CYCLE
                  │  streaks tracked   │         (loop continues
                  └────────────────────┘          indefinitely)
```

## Orchestrator Cycle (hourly)

Each hour, the orchestrator runs this sequence:

1. **Load registry** — `tasks/registry.yaml` lists enabled tasks
2. **Pre-pass** — detect crashed/timed-out tasks from prior cycles
3. **Self-heal** — for each crash: diagnose → fix → retry (reactive)
4. **Schedule check** — is each task due? (hourly/daily/weekly)
5. **Preflight** — are prerequisites met? (files, env vars, network)
6. **Spawn** — launch due tasks as subprocesses
7. **Wait/async** — wait for short tasks (≤15 min); leave long ones running
8. **Retry pass** — re-spawn crashed daily tasks (max 3 retries/day)
9. **Reflection** — at 4 AM: multi-day trend analysis, durable fixes (proactive)
10. **Digest** — at 5 AM: collect summaries, inject health report, send email

## Three Execution Modes

### Agent mode (`execution.agent: "codex"` or `"claude"`)

The orchestrator spawns a full CLI agent session. The agent reads `task.md`
and autonomously decides what to do — browse the web, write code, run commands,
iterate. The LLM is the workflow driver.

```
orchestrator ──▶ codex exec / claude -p ──▶ agent reads task.md
                                            agent decides actions
                                            agent writes summary
```

**When to use:** New tasks, ambiguous problems, creative work.
**LLM cost:** High (full context every cycle).

### Direct mode (`execution.agent: "direct"`)

The orchestrator runs a Python script (`run.py`) directly. The script controls
the workflow deterministically. LLM calls are made selectively via
`call_llm_json()` for steps that genuinely need reasoning.

```
orchestrator ──▶ python run.py ──▶ load_state()        ← pure Python
                                   call_llm_json(...)   ← one LLM call
                                   synthesize(...)       ← one LLM call
                                   save_state()          ← pure Python
                                   write_summary()       ← pure Python
```

**When to use:** Stable, well-understood tasks.
**LLM cost:** Low (only the calls you choose to make).

### Hybrid mode (`execution.agent: "direct"` + self-healing)

Same as direct mode, but when the script crashes, the orchestrator's
self-healing system spawns a diagnostic agent to read logs, identify the
root cause, apply a fix, and retry. No extra configuration needed.

**When to use:** Mostly stable tasks in changing environments.
**LLM cost:** Low normally; LLM invoked only on failure.

## Self-Healing Layer

### Reactive (per-crash)

When a task crashes or times out, the orchestrator immediately:
1. Spawns a diagnostic CLI agent with crash logs + task code
2. Agent identifies root cause and applies code/config fix
3. Orchestrator retries the task (up to 3 times per day)
4. On retry #2+, re-diagnoses with prior failure context

### Proactive reflection (daily, 4 AM)

Once per day, a reflection pipeline analyzes the past 7 days:
1. **Collect** — task health, failure patterns, engagement, email logs
2. **Analyze** — LLM-backed failure pattern analysis, engagement trends, value assessment
3. **Act** — spawn durable fix agents, run smoke tests, track fix outcomes
4. **Report** — generate health dashboard injected into the daily digest
5. **Persist** — multi-day state: failure streaks, fix history, engagement trends

See [charter_worker/proactive/reflection/DESIGN.md](../charter_worker/proactive/reflection/DESIGN.md).

## CLI Provider Abstraction

All LLM calls — task spawning, diagnosis, reflection analysis, durable fixes —
go through a unified provider abstraction (`charter_worker/proactive/llm.py`).

The provider is selected via:
```
CHARTER_LLM_CLI env var  →  charter.yaml execution.agent  →  auto-detect
```

Built-in support for Codex and Claude Code. Custom CLIs (Gemini Code, OpenCode,
etc.) supported via `CHARTER_LLM_CMD_TEMPLATE` env var. See README for details.

## Proactive Research Agent

5-phase autonomous research cycle with 10 guardrails, used by research tasks:

| Phase | Module | What |
|-------|--------|------|
| 1. Context | `phase_context.py` | Load state, parse email replies, detect stagnation |
| 2. Research | `phase_research.py` | Generate queries, dedup, web search |
| 3. Synthesize | `phase_synthesize.py` | Extract claims, check provenance/novelty, update hypothesis |
| 4. Feedback | `phase_feedback.py` | Self-review, compose email report |
| 5. Speculate | `phase_speculate.py` | Devil's advocate, lightweight exploration |

## Key Files

```
charter-worker/                          # Framework
  orchestrator.py                        # Main loop (hourly cron entry point)
  preflight.py                           # Constraint checker
  charter_worker/
    proactive/llm.py                     # CLI-agnostic LLM abstraction
    proactive/reflection/                # Self-reflection system
    proactive/phase_*.py                 # 5-phase research cycle
    proactive/guardrails.py              # G1-G12 quality guardrails
    executor/agent.py                    # CLI agent session launcher
    comm/email.py                        # Email sender
    research/engine.py                   # Deep research (fan-out/fan-in)

your-instance/                           # Your tasks (separate repo)
  tasks/registry.yaml                    # Enabled tasks list
  tasks/my_task/charter.yaml             # Task schedule + execution config
  tasks/my_task/run.py                   # Direct-mode entry point
  tasks/my_task/task.md                  # Agent-mode instructions
  tasks/my_task/state/                   # Task-local persistent state
  orchestrator_state.json                # Runtime state (auto-managed)
  reflection_state.json                  # Multi-day reflection state
  daily_summaries/YYYY-MM-DD/            # Per-day outputs (ephemeral)
```
