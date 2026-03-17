# charter-worker

A framework for running autonomous, recurring personal tasks via CLI agents (Codex CLI, Claude Code).

Charter-worker is the **engine** — it orchestrates tasks, manages schedules and locks, runs preflight checks, dispatches work to CLI agents, and sends email digests. Your tasks and data live in a separate **instance directory** (your own repo).

```
┌─────────────────────────────────────┐
│         charter-worker (this repo)  │
│                                     │
│  orchestrator.py   ← hourly cron   │
│  preflight.py      ← constraint    │
│  charter_worker/   ← modules       │
│    comm/           ← email          │
│    executor/       ← agent sessions │
│    proactive/      ← research loop  │
│    research/       ← deep research  │
│    utils/          ← helpers        │
└────────────┬────────────────────────┘
             │ reads tasks/registry.yaml
             │ spawns codex exec / claude -p
             ▼
┌─────────────────────────────────────┐
│     your-instance/ (separate repo)  │
│                                     │
│  tasks/registry.yaml                │
│  tasks/my_task/charter.yaml         │
│  tasks/my_task/task.md              │
│  email_config.yaml                  │
│  daily_summaries/                   │
└─────────────────────────────────────┘
```

---

## Features

- **Schedule-based orchestration** — hourly, daily, or weekly tasks, each with lock files to prevent double-spawning
- **Preflight constraints** — skip tasks when prerequisites aren't met (missing files, agent not installed, network down)
- **CLI agent dispatch** — spawns `codex exec` or `claude -p` subprocesses for each task, with prompt piped via stdin
- **Email feedback loop** — tasks send reports, read your replies via IMAP, and adjust their behavior
- **Proactive research agent** — 5-phase autonomous research cycle with 10 guardrails (deduplication, relevance, provenance, novelty, stagnation detection)
- **Experiment executor** — multi-step experiment runner with auto-retry, validation, and follow-up planning
- **Deep research engine** — fan-out/fan-in research pipeline (planner → parallel workers with web search → aggregator → reviewer)
- **Daily digest** — collects all task summaries and emails a single digest, including late-arriving results from the prior day

---

## Install

```bash
pip install -e /path/to/charter-worker/

# Or from GitHub:
pip install git+https://github.com/youruser/charter-worker.git
```

This installs the `charter-orchestrator` CLI command.

---

## Quick Start

### 1. Create an instance directory

```bash
mkdir my-tasks && cd my-tasks
git init
```

### 2. Create the task registry

```yaml
# tasks/registry.yaml
version: 2
tasks:
  - id: "my_research"
    enabled: true
    path: "tasks/my_research"
```

### 3. Create a task

```bash
mkdir -p tasks/my_research/state
```

**`tasks/my_research/charter.yaml`**:
```yaml
task_id: "my_research"
name: "Daily Research Digest"

schedule:
  frequency: "daily"
  max_runtime_minutes: 15

execution:
  agent: "codex"
  sandbox: "none"         # "none" for full access, omit for sandboxed

# Optional: skip task if requirements aren't met
constraints:
  command_available: "codex"
  file_exists: "email_config.yaml"

report:
  digest: true
```

**`tasks/my_research/task.md`**:
```markdown
# My Research Task

You are a research assistant. Each cycle:

1. Read `state/state.json` to see what you've done before
2. Search for recent papers on [your topic]
3. Write a brief summary of what you found
4. Update `state/state.json` with today's findings
5. Write summary.md and summary.json to the output path given in your prompt
```

### 4. Set up email (optional)

Create `email_config.yaml` in your instance root (add to `.gitignore`):

```yaml
sender:
  address: "yourbotaccount@gmail.com"
  app_password: "xxxx xxxx xxxx xxxx"    # Gmail App Password (not your login password)
  smtp_server: "smtp.gmail.com"
  smtp_port: 587

recipient_allowlist:
  - "you@gmail.com"

rate_limit:
  max_sends_per_day: 20
  cooldown_seconds: 30

enabled: true
```

### 5. Run

```bash
# Set instance root (or use --instance-dir)
export CHARTER_INSTANCE_ROOT=/path/to/my-tasks

# Dry run — see what would execute
charter-orchestrator --dry-run

# Real run — spawns agents for due tasks
charter-orchestrator

# Force-run specific tasks regardless of schedule
charter-orchestrator --force my_research

# Force-run everything
charter-orchestrator --force
```

### 6. Automate (cron / Task Scheduler)

**Linux/macOS cron** (hourly):
```bash
0 * * * * CHARTER_INSTANCE_ROOT=/path/to/my-tasks charter-orchestrator >> /path/to/my-tasks/cron.log 2>&1
```

**Windows Task Scheduler** (hourly, via WSL2):
```powershell
$action = New-ScheduledTaskAction -Execute "wsl.exe" `
  -Argument "-d Ubuntu -- /path/to/my-tasks/run.sh"
$trigger = New-ScheduledTaskTrigger -Once -At 0:00AM `
  -RepetitionInterval (New-TimeSpan -Hours 1) `
  -RepetitionDuration (New-TimeSpan -Days 1)
Register-ScheduledTask -TaskName "CharterWorker" `
  -Action $action -Trigger $trigger
```

---

## Charter YAML Reference

Each task has a `charter.yaml` that the orchestrator reads:

```yaml
task_id: "my_task"
name: "Human-readable name"

# --- Orchestrator section ---
schedule:
  frequency: "daily"          # "hourly" | "daily" | "weekly"
  run_day: "Monday"           # weekly only
  run_hour: 6                 # daily only — skip before this hour
  max_runtime_minutes: 60

execution:
  agent: "codex"              # "codex" | "claude"
  sandbox: "none"             # "none" → full access, omit → sandboxed (codex --full-auto)

constraints:                  # all optional
  command_available:           # CLI tools that must exist on PATH
    - "codex"
  file_exists:                 # paths (relative to instance root, or absolute)
    - "email_config.yaml"
    - "tasks/shared_creds/token.json"
  env_var:                     # environment variables that must be set
    - "OPENAI_API_KEY"
  network_reachable:           # host:port that must be TCP-reachable
    - "smtp.gmail.com:587"

# --- Sub-agent section (read by the spawned agent, not the orchestrator) ---
context:
  description: "What this task does (shown to the agent)"
  instructions_file: "task.md"

report:
  digest: true                 # include in daily digest email
  own_email:
    enabled: true              # send a separate per-task email
    prefix: "[MY-TASK]"        # subject prefix for threading + IMAP filtering
    on: "always"               # "always" | "on_change" | "on_error"
```

---

## Modules

### `orchestrator.py` — The Scheduler

The main loop. Each cycle:
1. Loads `tasks/registry.yaml`
2. For each enabled task, checks schedule → preflight → lock
3. Spawns due tasks as `codex exec` or `claude -p` subprocesses
4. Waits for short tasks (≤15 min), leaves long ones async
5. Sends a daily digest email (first cycle of the day)

### `preflight.py` — Constraint Checker

Validates task prerequisites before spawning:
- `command_available` — checks `shutil.which()`
- `file_exists` — checks path relative to instance root
- `env_var` — checks `os.environ`
- `network_reachable` — TCP connect with 5s timeout
- Auto-infers agent availability from `execution.agent`

### `charter_worker.comm` — Email

- **`email.py`** — rate-limited, audit-logged email sender (SMTP). Markdown → HTML conversion. Configurable via `email_config.yaml` or `CHARTER_EMAIL_CONFIG` env var.
- **`digest.py`** — collects task summaries, converts to PDF attachments, sends daily digest.

### `charter_worker.executor` — Agent Sessions

- **`agent.py`** — launches a full CLI agent session (`codex exec` or `claude -p`) in a workspace, reads back a structured result JSON.
- **`cycle.py`** — 5-phase experiment executor: context → execute → analyze → report → plan next. Each phase launches a separate agent session.

### `charter_worker.proactive` — Research Agent

A 5-phase autonomous research loop with 10 guardrails:

| Phase | What it does |
|-------|-------------|
| **1. Context** | Load project state, check email replies, parse feedback, promote speculative work, detect stagnation (G7) |
| **2. Research** | Generate queries from open questions, dedup (G2), run web searches or deep research |
| **3. Synthesize** | Extract claims, check provenance (G3), gate relevance (G1), check novelty (G4), enforce size caps (G5), update hypothesis |
| **4. Feedback** | Self-review (G6), compose threaded email, send report |
| **5. Speculate** | Lightweight exploration of top action directions, stored in isolated buffer (G9) |

Guardrails:
| ID | Name | Type | Purpose |
|----|------|------|---------|
| G1 | Relevance Gate | LLM | Score claims against project goal |
| G2 | Dedup Gate | Programmatic | Skip queries similar to recent ones |
| G3 | Provenance Check | Programmatic | Label unsourced claims as [HYPOTHESIS] |
| G4 | Novelty Check | LLM | Flag when all suggestions are stale |
| G5 | Size Cap | Programmatic | Evict lowest-scoring items when lists grow too long |
| G6 | Self-Review | LLM | Pre-send quality check on the email report |
| G7 | Stagnation Detection | Programmatic | Flag when 2+ cycles show no progress |
| G9 | Speculative Isolation | Programmatic | Ensure speculative findings don't leak into main status |
| G10 | Feedback Integration | LLM | Parse human replies into structured corrections/commands |

### `charter_worker.research` — Deep Research Engine

Fan-out/fan-in pipeline for answering complex questions:
1. **Planner** — decomposes a question into 5-7 subquestions
2. **Workers** (parallel) — each researches one subquestion via `codex exec --search`
3. **Aggregator** — synthesizes findings, resolves conflicts
4. **Reviewer** — quality grades (A-D), identifies gaps and unsupported claims

```python
from charter_worker.research.engine import run_research

result = run_research(
    "What is the state of the art in tabular data generation?",
    output_dir="./research_output",
    max_workers=3,
)
```

---

## Email Feedback Loop

Tasks can communicate with you via email. You can:

1. **Reply to a task email** — the task reads your reply on the next cycle and adjusts
2. **Send a fresh email** to the bot address with the task's subject prefix (e.g. `[MY-TASK] please focus on X`) — picked up the same way

The proactive research agent supports structured feedback:
- **Corrections**: "Actually X is wrong, it should be Y"
- **Rejections**: "Don't pursue X"
- **Commands**: "pause", "done", "resume", "focus on X"
- **New priorities**: "I also want to look at Y"

---

## Steering a Running Task

Tasks are autonomous but steerable. There are several ways to adjust what a task does, from most direct to most hands-off:

### 1. Edit the state file directly

Each task keeps its state in `tasks/<task_id>/state/` (typically `state.json` or `experiment_state.json`). This is the most direct control surface. The agent reads it at the start of every cycle.

For experiment-style tasks, the state file contains:

```json
{
  "status": "implementing",
  "pending_steps": ["tune_temperature", "run_larger_dataset", "write_report"],
  "completed_steps": ["install_packages", "smoke_test", "run_experiments"],
  "failed_steps": ["tabebm_incompatible"],
  "notes": "what happened last cycle"
}
```

**Step names are free-form strings** — the agent invents them while building its plan. There is no separate step definition file. The state file IS the definition. You can:
- Add, remove, or reorder items in `pending_steps`
- Change `status` (e.g. from `"validating"` back to `"implementing"`)
- Clear `failed_steps` to retry something
- Add context in `notes` that the agent will read

### 2. Edit the plan file

Experiment tasks typically write a plan to their workspace (e.g. `experiments/plan.md`). The agent reads this for context on what to do and why. You can edit it to change datasets, metrics, approach, or add entirely new sections.

### 3. Send an email

Compose an email to the bot address with the task's subject prefix. No prior email thread needed:

```
To: yourbotaccount@gmail.com
Subject: [EXP-TABPFN] Skip TabEBM, tune temperature on breast_cancer instead
Body: The TabEBM path is blocked. Focus on hyperparameter tuning...
```

The task picks this up via IMAP on the next cycle and adjusts its plan.

### 4. Edit `task.md`

`tasks/<task_id>/task.md` contains the instructions the agent receives every cycle. Change the overall workflow, rules, or behavioral constraints here. This is the "constitution" — it shapes all future cycles.

### 5. Reply to a task email

When a task emails you a report, just reply. The task reads your reply on the next cycle using the same IMAP mechanism as proactive emails.

---

## Long-Running Tasks and the Hourly Cron

### How lock files prevent double-spawning

When the orchestrator spawns an agent, it creates a `.lock` file in the task directory containing `{pid, started_at, max_runtime_minutes}`. On subsequent cron cycles:

1. Orchestrator checks `.lock` — does it exist?
2. If yes, checks if the PID is still alive (`kill -0`)
3. If alive → **task is skipped** ("locked, still running") — the existing session continues undisturbed
4. If the process died but hasn't exceeded `max_runtime_minutes` → removes stale lock (process crashed)
5. If exceeded → removes stale lock and logs a timeout warning

This means a task can safely run for hours. The hourly cron will just skip it each time it checks.

### One session does many steps

Each cycle spawns **one long `codex exec` session** (not one step). The `task.md` instructs the agent to "do as many steps as you can" — so it reads `pending_steps`, works through them sequentially, and saves state between steps. If the session hits the agent's context limit or a hard timeout, it saves state and the next cycle picks up where it left off.

### Configuring the timeout

Set `max_runtime_minutes` in `charter.yaml` to match your task's expected duration:

```yaml
schedule:
  frequency: "hourly"
  max_runtime_minutes: 180    # allow up to 3 hours before considering it stale
```

If a task regularly needs more than the configured timeout, increase this value. The lock file will protect it for that duration. After expiry, the orchestrator assumes the process died and cleans up the lock so the next cycle can start fresh.

### What if experiments take many hours?

For truly long experiments (training runs, large-scale evaluations), the recommended pattern is:

1. The agent **launches the experiment as a background process** (e.g. `nohup python train.py &`)
2. Saves the PID or output path in state
3. Exits the codex session (releasing the lock)
4. On the next cycle, checks if the background process finished and processes the results

This way the experiment runs independently while the agent session stays short. The orchestrator doesn't need to know about the background process — the task manages it through its state file.

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `CHARTER_INSTANCE_ROOT` | Instance directory (where `tasks/` lives) | Current working directory |
| `CHARTER_EMAIL_CONFIG` | Path to `email_config.yaml` | `$CHARTER_INSTANCE_ROOT/email_config.yaml` |

---

## Examples

See [`templates/`](templates/) for starter files:
- `charter.yaml.example` — annotated charter template
- `tasks.yaml.example` — annotated registry template

A minimal working example:

```
my-instance/
├── email_config.yaml          # gitignored
├── tasks/
│   ├── registry.yaml
│   └── hello_world/
│       ├── charter.yaml
│       ├── task.md
│       └── state/
└── daily_summaries/           # auto-generated
```

**`tasks/registry.yaml`**:
```yaml
version: 2
tasks:
  - id: "hello_world"
    enabled: true
    path: "tasks/hello_world"
```

**`tasks/hello_world/charter.yaml`**:
```yaml
task_id: "hello_world"
name: "Hello World"
schedule:
  frequency: "daily"
  max_runtime_minutes: 5
execution:
  agent: "codex"
  sandbox: "none"
report:
  digest: true
```

**`tasks/hello_world/task.md`**:
```markdown
# Hello World Task

1. Read state/state.json (create if missing, start with {"run_count": 0})
2. Increment run_count
3. Save state/state.json
4. Write summary.md with: "Hello! This is run #N."
5. Write summary.json with status "success"
```

Run it:
```bash
export CHARTER_INSTANCE_ROOT=/path/to/my-instance
charter-orchestrator --force hello_world
```

---

## Requirements

- Python 3.10+
- [Codex CLI](https://github.com/openai/codex) and/or [Claude Code](https://claude.ai/code) installed
- `pyyaml>=6.0`
- Optional: `markdown>=3.4`, `weasyprint>=60.0` (for PDF email attachments)

---

## License

MIT
