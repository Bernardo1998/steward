# Orchestrator Self-Reflection and Improvement System

## What It Does

The reflection system is a daily proactive pipeline that treats the orchestrator
itself as a project to improve. It runs once per day at 4 AM (before the 5 AM
digest) and answers three questions:

1. **What keeps breaking and why?** (failure pattern analysis)
2. **Is the user actually using the output?** (engagement analysis)
3. **What should the system do differently tomorrow?** (action planning)

## When It Runs

- **Trigger**: Orchestrator cycle at hour >= 4, once per day
- **Duration**: 5-45 minutes depending on whether fixes are needed
- **Budget**: 3 LLM calls for analysis + 0-2 fix agents for durable repairs

## Pipeline Phases

### Phase 1: Collect (`collector.py`)
Gathers data from the past 7 days. No LLM calls.

**Data sources**:
- `orchestrator_state.json` — per-task run history, retry counts, diagnoses
- `daily_summaries/*/tasks/*/summary.json` — task outcomes over 7 days
- `tasks/_shared/state/email_send_log.jsonl` — email delivery health
- `tasks/*/state/task_state.json` — proactive task engagement (days_since_reply)
- `tasks/*/logs/cycle_*.log` — raw error text for persistent failures
- `reflection_state.json` — prior fix outcomes, engagement trends

**Output**: `ReflectionContext` dict containing per-task health records,
engagement records, cross-task system patterns, and email infrastructure health.

### Phase 2: Analyze (`analyzer.py`)
Three LLM-backed analysis passes.

**Pass 1 — Failure Patterns** (programmatic grouping + 1 LLM call):
- Groups failures by error similarity across tasks and days
- Detects failure streaks (> 3 consecutive days)
- Identifies cross-task patterns (e.g., "diagnostic timeout" affecting 3 tasks)
- For persistent patterns: LLM generates root cause hypothesis + durable fix,
  given the history of reactive fixes already tried

**Pass 2 — Engagement** (1 LLM call):
- For tasks with email feedback loops: trend detection (increasing/declining)
- Report quality assessment from recent summaries
- Suggested adjustments (simplify, reduce frequency, change format)

**Pass 3 — Value Assessment** (1 LLM call):
- Per-task value tier (high/medium/low/negative)
- Based on: success rate, engagement, artifacts produced, action items

### Phase 3: Act (`actor.py`)
Executes remediation actions. In priority order:

1. **Durable code/config fixes** — for 3+ day failure streaks, spawn a Claude
   Code agent with enriched prompt that includes multi-day diagnosis history and
   explicitly states which fixes were already tried and failed. Max 2 per run.

2. **Smoke tests** — after fixes, run `python run.py get-project` or
   task-specific commands. Pass/fail recorded in state.

3. **Config adjustments** — for engagement issues, modify report
   frequency/verbosity parameters in task config files.

4. **Disable recommendations** — for 7+ day failures with 5+ fixes tried,
   flag in health report (never auto-disable without explicit charter config).

**Guardrails**:
- G11 (Fix Regression Check): reject fixes similar to ones that already failed
- G12 (Disable Safety Check): gate auto-disable behind multiple conditions

### Phase 4: Report (`report.py`)
Generates markdown injected into the daily digest.

### Phase 5: Persist (`state.py`)
Updates `reflection_state.json` with fix outcomes, engagement history, patterns.

## Expected Output

### In the Daily Digest

A new "System Health Report" section appears at the top:

```markdown
## System Health Report

### Task Status (7-day window)
| Task | Success Rate | Status | Days Failing | Action Taken |
|------|-------------|--------|-------------|--------------|
| paper_reader | 14% (1/7) | failing | 7 | Durable fix applied |
| health_assistant | 57% (4/7) | failing | 3 | Smoke test passed |
| daily_planner | 100% (7/7) | healthy | 0 | -- |
| job_search | 57% (4/7) | failing | 3 | Config adjusted |

### Patterns Detected
- **Diagnostic agent timeout** (3 tasks): The diagnostic agent itself
  exhausts its 600s budget reading complex logs. → Recommend increasing
  _DIAGNOSE_TIMEOUT or simplifying diagnostic prompt.
- **arXiv rate limiting** (paper_reader, 7 days): API returns 429s
  during peak hours. → Applied exponential backoff + web search fallback.

### Fixes Applied This Morning
1. paper_reader: Added arXiv retry with exponential backoff in
   scripts/arxiv_client.py. Smoke test (`python run.py get-project`): PASS.
2. job_search: Increased max_runtime_minutes from 45 to 60 in charter.yaml.
   Smoke test: PASS.

### Engagement Trends
- ltt_agent_structure: No user reply in 8 days. Report may be too dense.
  Consider: simplify to 3 bullet points + 1 question.
- health_assistant: User replied 3 days ago. Engagement stable.

### Recommendations (requires human action)
- [ ] Investigate diagnostic agent timeout pattern (systemic, affects 3 tasks)
- [ ] Review paper_reader if it fails again despite today's fix
- [ ] Consider reducing job_search email frequency (user hasn't replied in 5 days)
```

### In `reflection_state.json`

Persistent state that accumulates across days:

```json
{
  "last_reflection_date": "2026-03-29",
  "reflection_count": 1,
  "diagnosis_history": {
    "paper_reader": [
      {
        "date": "2026-03-29",
        "diagnosis": "arXiv rate limiting + Sunday scheduling",
        "fix_applied": true,
        "fix_desc": "Sunday=off, backoff in arxiv_client.py",
        "outcome": "pending"
      }
    ]
  },
  "failure_streaks": {
    "paper_reader": {"start": "2026-03-22", "days": 7, "fixes_tried": 4}
  },
  "applied_fixes": [
    {
      "id": "fix_20260329_paper_reader_backoff",
      "date": "2026-03-29",
      "task": "paper_reader",
      "description": "Added exponential backoff to arxiv_client.py",
      "smoke_test": "pass",
      "outcome": "pending",
      "days_until_recurrence": null
    }
  ],
  "engagement_history": {
    "ltt_agent_structure": [
      {"date": "2026-03-29", "days_since_reply": 8, "report_quality": 3}
    ]
  },
  "task_value_tiers": {
    "paper_reader": {"tier": "high", "rationale": "Feeds vault, drives reading", "assessed": "2026-03-29"}
  },
  "patterns": [
    {
      "id": "diagnostic_timeout",
      "first_seen": "2026-03-26",
      "last_seen": "2026-03-29",
      "occurrences": 8,
      "affected_tasks": ["health_assistant", "job_search", "ltt_agent_structure"],
      "status": "open",
      "recommendation": "Increase _DIAGNOSE_TIMEOUT or simplify prompt"
    }
  ]
}
```

### In the Orchestrator Log (`run.log`)

```
[orch] Running daily reflection for 2026-03-29...
  [reflect] Loaded 7 days of data for 6 tasks
  [reflect] Failure patterns: 2 detected (diagnostic_timeout, arxiv_rate_limit)
  [reflect] Engagement: ltt_agent_structure declining (8 days no reply)
  [reflect] Spawning durable fix agent for paper_reader...
  [reflect] Fix applied: backoff in arxiv_client.py
  [reflect] Smoke test paper_reader: PASS
  [reflect] Health report generated (5 sections)
  [reflect] Reflection complete in 312s
```

## What It Does NOT Do

- Does not modify `orchestrator.py` itself (system-level issues flagged for human action)
- Does not auto-disable tasks unless `reflection.auto_disable: true` in charter.yaml
- Does not retry tasks (that's the existing retry system's job)
- Does not send separate emails (output goes into the daily digest only)
- Does not replace reactive self-healing (runs in addition to it, addresses different timescale)

## How Fix Outcomes Are Tracked

1. Reflection applies a fix and records it in `reflection_state.json` with `outcome: "pending"`
2. Next day's reflection checks: did the task succeed today?
   - Yes → `outcome: "success"`, `days_until_recurrence: null`
   - No → `outcome: "failed"`, pattern escalated
3. G11 guardrail prevents re-applying fixes that previously failed
4. After 3 failed fix attempts for the same pattern, it stops trying code fixes
   and flags the issue as "requires human intervention" in the health report
