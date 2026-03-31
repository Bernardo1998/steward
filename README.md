# charter-worker

**A governed autonomy engine for recurring tasks.**

Charter-worker turns repeated open-ended work into cheap, inspectable workflows.
It uses strong LLM reasoning only when uncertainty or failure actually requires
it — for search, ambiguity, recovery, and redesign. Everything else runs as
direct scripts.

This is **amortized autonomy**: start in exploratory agent mode, then promote
stable work to routine execution. The orchestrator handles scheduling, locks,
preflight checks, self-healing, and email reporting.

> **Research preview.** This system runs the author's daily workflow (research,
> experiments, health tracking, job search) and is grounded in organizational
> theory (Coase, Simon, Galbraith). See [docs/theory.md](docs/theory.md) for
> the theoretical motivation and [ROADMAP.md](ROADMAP.md) for what's next.

```
charter-worker (this repo)              your-instance/ (separate repo)
┌─────────────────────────────┐         ┌───────────────────────────────┐
│ orchestrator.py  ← cron     │ reads → │ tasks/registry.yaml           │
│ charter_worker/  ← modules  │         │ tasks/my_task/charter.yaml    │
│ preflight.py     ← checks   │ spawns  │ tasks/my_task/run.py or       │
│ init_cmd.py      ← bootstrap│ ────→   │ tasks/my_task/task.md         │
│ status_cmd.py    ← status   │         │ email_config.yaml             │
│ templates/       ← starters │         │ daily_summaries/              │
└─────────────────────────────┘         └───────────────────────────────┘
```

---

## Quick Start

### Option A: Zero-dependency first run (recommended)

```bash
pip install -e /path/to/charter-worker/
charter-init ./my-tasks
cd my-tasks
charter-orchestrator --force hello_world
```

This creates an instance with the `hello_world` template — a pure Python task
that counts files and writes a summary. No Codex, no Claude, no API keys.

### Option B: Agent-backed task

```bash
charter-init ./my-tasks --template ltt_thinker
cd my-tasks
# Requires: codex CLI installed and authenticated
charter-orchestrator --force ltt_thinker
```

### Option C: AI agent setup

Point your AI agent at [`docs/agent-setup.md`](docs/agent-setup.md) — it
contains step-by-step instructions to bootstrap a working instance automatically.

---

## Three Execution Modes

Charter-worker supports **workflow promotion** — tasks graduate from expensive
agent reasoning to cheap direct execution as they stabilize:

| Mode | `execution.agent` | LLM cost | When to use |
|------|-------------------|----------|-------------|
| **Exploratory** | `codex` or `claude` | High (every cycle) | New, ambiguous, creative tasks |
| **Routine** | `direct` | None (pure script) | Stable, well-understood tasks |
| **Hybrid** | `direct` + self-healing | Low (LLM on failure only) | Mostly stable, changing environment |

**Exploratory** mode wraps the entire task in an LLM agent session — the agent
reads `task.md` and autonomously decides what to do. **Routine/direct** mode
runs a Python script (`run.py`) that controls the workflow deterministically and
only calls the LLM for specific reasoning steps (e.g., synthesis, evaluation).

See [docs/workflow-promotion.md](docs/workflow-promotion.md) for the full guide.

---

## How It Differs

### vs OpenClaw / persistent autonomous agents

OpenClaw emphasizes persistent autonomous iteration. Charter-worker emphasizes
**governed and amortized autonomy**: repeated work should be routinized into
explicit workflows, while LLM reasoning is used selectively.

### vs autoresearch / FARS / ARIS

These are fixed research-only pipelines (search → read → summarize).
Charter-worker handles any recurring task, with search and action interleaved,
plus experiment dispatch, email feedback, and self-healing.

See [docs/comparison.md](docs/comparison.md) for details.

---

## Features

- **Schedule-based orchestration** — hourly, daily, or weekly, with lock files to prevent double-spawning
- **CLI-agent agnostic** — works with Codex, Claude Code, or any custom CLI agent
- **Reactive self-healing** — when tasks crash, a diagnostic agent reads logs, applies fixes, and retries
- **Proactive self-reflection** — daily analysis of multi-day failure patterns, engagement, and task value
- **Preflight constraints** — skip tasks when prerequisites aren't met
- **Email feedback loop** — tasks send reports; reply to steer behavior
- **Proactive research agent** — 5-phase cycle with 10 guardrails for long-horizon coherence
- **Experiment dispatcher** — multi-step experiment runner with budget tracking
- **Deep research engine** — fan-out/fan-in pipeline (planner → parallel workers → aggregator → reviewer)
- **Daily digest** — collects all task summaries (with system health report) into a single email
- **Status surface** — `charter-status` shows all tasks, states, and latest results

---

## Install

```bash
pip install -e /path/to/charter-worker/

# Or from GitHub:
pip install git+https://github.com/Bernardo1998/charter-worker.git
```

This installs three CLI commands:
- `charter-orchestrator` — run the scheduling loop
- `charter-init` — bootstrap a new instance
- `charter-status` — show task status

---

## Usage

```bash
# Bootstrap a new instance
charter-init ./my-tasks
charter-init ./my-tasks --template ltt_thinker
charter-init --list-templates

# Run tasks
export CHARTER_INSTANCE_ROOT=./my-tasks
charter-orchestrator --dry-run          # see what would run
charter-orchestrator                    # run due tasks
charter-orchestrator --force my_task    # force-run specific task

# Check status
charter-status
charter-status --json
charter-status --output status.md
```

### Automate (cron)

```bash
# Linux/macOS — hourly
0 * * * * CHARTER_INSTANCE_ROOT=/path/to/my-tasks charter-orchestrator >> /path/to/cron.log 2>&1
```

---

## CLI Provider Configuration

Charter-worker is **CLI-agent agnostic**. Any coding agent that accepts a prompt
and can read/write files will work. The provider is selected at three levels:

```
CHARTER_LLM_CLI env var  →  charter.yaml execution.agent  →  auto-detect
    (global default)           (per-task override)           (codex > claude)
```

### Built-in providers

| Provider | Binary | Exploratory mode | Write mode |
|----------|--------|-----------------|------------|
| **Codex** | `codex` | `codex exec --ephemeral -s read-only` | `codex exec --dangerously-bypass-approvals-and-sandbox` |
| **Claude Code** | `claude` | `claude -p <prompt> --dangerously-skip-permissions` | `claude -p <prompt> --dangerously-skip-permissions` |

### Switching providers

```bash
# Use Claude Code for everything (global)
export CHARTER_LLM_CLI=claude

# Use Codex for everything (default if both are installed)
export CHARTER_LLM_CLI=codex

# Per-task override in charter.yaml (takes precedence)
execution:
  agent: "claude"    # this task uses Claude Code regardless of env var
```

### Custom providers (Gemini Code, OpenCode, etc.)

Any CLI that accepts a prompt and produces text output can be used. Set two
environment variables:

```bash
# 1. Tell charter-worker which binary to use
export CHARTER_LLM_CLI=gemini-code

# 2. Tell it how to build commands for your CLI
export CHARTER_LLM_CMD_TEMPLATE='{
  "read_only": {
    "cmd": ["gemini-code", "--non-interactive", "--read-only"],
    "stdin": false,
    "prompt_flag": "--prompt"
  },
  "write": {
    "cmd": ["gemini-code", "--non-interactive", "--auto-approve"],
    "stdin": false,
    "prompt_flag": "--prompt"
  },
  "model_flag": ["--model"],
  "workdir_flag": ["-C"],
  "adddir_flag": ["--add-dir"]
}'
```

The template defines two modes (`read_only` for analysis, `write` for code changes),
how the prompt is passed (`stdin: true` to pipe, `stdin: false` with a `prompt_flag`),
and the flag names for model, working directory, and additional directory access.

This abstraction is used everywhere: task spawning, reactive self-healing diagnosis,
proactive reflection fix agents, and all internal LLM analysis calls.

---

## Charter YAML Reference

Each task has a `charter.yaml` that the orchestrator reads:

```yaml
task_id: "my_task"
name: "Human-readable name"

schedule:
  frequency: "daily"          # "hourly" | "daily" | "weekly"
  run_day: "Monday"           # weekly only
  run_hour: 6                 # daily only — skip before this hour
  max_runtime_minutes: 60

execution:
  agent: "direct"             # "codex" | "claude" | "direct"
  entrypoint: "python run.py" # required for "direct" mode
  sandbox: "none"             # codex only: "none" for full access

constraints:                  # all optional
  command_available: ["codex"]
  file_exists: ["email_config.yaml"]
  env_var: ["OPENAI_API_KEY"]

report:
  digest: true
  own_email:
    enabled: true
    prefix: "[MY-TASK]"
    on: "always"              # "always" | "on_change" | "on_error"
```

---

## Templates

| Template | Mode | Description |
|----------|------|-------------|
| `hello_world` | direct | Zero-dep demo: counts files, writes summary |
| `ltt_thinker` | codex | Proactive research agent with email feedback |
| `experiment_task` | codex | Multi-step experiment runner |

```bash
charter-init ./my-tasks --template hello_world    # default
charter-init ./my-tasks --template ltt_thinker
charter-init ./my-tasks --template experiment_task
```

---

## Architecture

### Orchestrator (`orchestrator.py`)

The main loop, run by cron every hour:
1. Load `tasks/registry.yaml`
2. For each task: check schedule → preflight → lock
3. Spawn due tasks as subprocesses (agent or direct mode)
4. Wait for short tasks; leave long ones async
5. Retry crashed tasks with **reactive self-healing** (diagnosis agent → fix → retry)
6. Run **daily reflection** at 4 AM (multi-day pattern analysis → durable fixes)
7. Send daily digest email at 5 AM (includes system health report)

### How LLM Calls Work in Each Mode

```
Exploratory mode (agent: "codex" or "claude")
┌──────────────────────────────────────────────────────┐
│  CLI agent session (codex exec / claude -p)          │
│  ┌────────────────────────────────────────────────┐  │
│  │  Agent reads task.md, autonomously decides     │  │
│  │  what to do: browse, code, run commands, etc.  │  │
│  │  The LLM IS the workflow driver.               │  │
│  └────────────────────────────────────────────────┘  │
│  Cost: full LLM context per cycle                    │
└──────────────────────────────────────────────────────┘

Direct mode (agent: "direct")
┌──────────────────────────────────────────────────────┐
│  Python script (run.py) drives the workflow          │
│  ┌────────────────────────────────────────────────┐  │
│  │  phase1: load_state()           ← pure Python  │  │
│  │  phase2: call_llm_json(...)     ← one LLM call │  │
│  │  phase3: synthesize(...)        ← one LLM call │  │
│  │  phase4: send_email()           ← pure Python  │  │
│  │  phase5: save_state()           ← pure Python  │  │
│  └────────────────────────────────────────────────┘  │
│  Cost: only the LLM calls you choose to make         │
└──────────────────────────────────────────────────────┘
```

In direct mode, your `run.py` calls `call_llm_json()` from `charter_worker.proactive.llm`
for steps that genuinely need reasoning. Everything else is deterministic Python.
See the [direct mode guide in guide.md](#) and the example below.

### Proactive Research Agent (`charter_worker.proactive`)

5-phase autonomous research cycle with 10 guardrails:

| Phase | What |
|-------|------|
| Context | Load state, parse email replies, detect stagnation |
| Research | Generate queries, dedup, web search |
| Synthesize | Extract claims, check provenance/novelty, update hypothesis |
| Feedback | Self-review, compose email report |
| Speculate | Lightweight exploration of top directions |

### Self-Reflection System (`charter_worker.proactive.reflection`)

Daily proactive pipeline that runs at 4 AM, treating the orchestrator itself
as a project to improve:

| Phase | What |
|-------|------|
| Collect | Gather 7-day data: task health, failures, engagement, email logs |
| Analyze | LLM-backed: failure patterns, engagement trends, task value tiers |
| Act | Spawn durable fix agents, run smoke tests, track fix outcomes |
| Report | Generate health dashboard for daily digest |
| Persist | Multi-day state: failure streaks, fix history, engagement trends |

See [charter_worker/proactive/reflection/DESIGN.md](charter_worker/proactive/reflection/DESIGN.md)
for expected behavior and output format.

### Deep Research Engine (`charter_worker.research`)

Fan-out/fan-in for complex questions:
Planner → parallel Workers (web search) → Aggregator → Reviewer

```python
from charter_worker.research.engine import run_research
result = run_research("What is the state of the art in X?", output_dir="./out")
```

---

## Organizational Theory Grounding

Charter-worker is not just tooling — it embodies ideas from organizational
economics about when to centralize vs. decompose work:

- **Coase**: Don't add costly agent boundaries unless they pay off
- **Simon**: Decompose along natural joints; routinize repeated decisions
- **Galbraith**: Match information-processing capacity to task uncertainty

The [agent-structure experiment](docs/theory.md) tests these predictions
empirically: when does single-agent beat multi-agent, and why?

---

## Documentation

| Doc | What |
|-----|------|
| [guide.md](guide.md) | Full setup and usage guide |
| [docs/workflow-promotion.md](docs/workflow-promotion.md) | Exploratory → routine promotion guide |
| [docs/theory.md](docs/theory.md) | Organizational theory motivation |
| [docs/comparison.md](docs/comparison.md) | vs OpenClaw, autoresearch, Airflow |
| [docs/limitations.md](docs/limitations.md) | Honest gaps and limitations |
| [docs/agent-setup.md](docs/agent-setup.md) | AI agent bootstrapping guide |
| [charter_worker/proactive/reflection/DESIGN.md](charter_worker/proactive/reflection/DESIGN.md) | Self-reflection system design |
| [ROADMAP.md](ROADMAP.md) | What works now and what's next |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |

---

## Requirements

- Python 3.10+
- For agent-backed tasks: [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://claude.ai/code)
- `pyyaml>=6.0`
- Optional: `markdown>=3.4`, `weasyprint>=60.0` (PDF email attachments)

---

## License

MIT
