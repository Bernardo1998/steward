# Steward

**Delegate work, not attention.**

A workflow-hardening layer for Claude Code, Codex, and other CLI agents.
Start with a full agent when the task is fuzzy. Get async progress reports
instead of constant interruptions. As the task stabilizes, crystallize the
repeatable parts into scripts — keeping LLM calls only where language and
judgment are actually needed.

*"Not full agent forever. Delegate first, crystallize what repeats."*

![Steward system overview](docs/steward_workflow.png)

---

## What It Does

```bash
# Add a task from plain English
steward-add-task "Send me a daily digest of new papers on LLM agent eval"

# It runs on a cron loop — no human intervention needed
steward --force paper_watch

# When the workflow stabilizes, crystallize it into a script
steward-promote paper_watch --last 10
```

**Two examples of what Steward manages daily:**

| Task | What it does | Mode |
|------|-------------|------|
| **Paper watch** | Search arXiv, score papers, deep-read top picks, update vault | Direct (scripted) |
| **Job follow-up** | Sync contacts, draft outreach, check replies, remind on stale leads | Direct (scripted) |

Both started as full-agent tasks. Both were promoted to cheap scripts after
their workflows stabilized. The agent runs only on failure (self-healing).

---

## Quick Start

### Let your AI agent set it up

Point your AI agent at [`docs/agent-setup.md`](docs/agent-setup.md) — it
contains step-by-step instructions to bootstrap a working instance.

### Manual setup

```bash
pip install -e /path/to/steward/
steward-init ./my-tasks
cd my-tasks
steward --force hello_world
```

### Add your own tasks

```bash
steward-add-task "Track my reading list and summarize weekly"
steward-add-task "Monitor competitor releases and alert me on changes"
```

### Automate

```bash
# Run every hour — Steward handles scheduling internally
0 * * * * STEWARD_INSTANCE_ROOT=/path/to/my-tasks steward >> cron.log 2>&1
```

---

## CLI Agent Support

Steward is **CLI-agent agnostic at every layer**. The same provider
abstraction governs three places where LLMs run:

1. **Task execution** — when the orchestrator spawns a task in agent mode
2. **Self-healing** — when the reflector spawns a fix agent for a broken task
3. **In-task LLM calls** — when your task code does deep research, web search,
   scoring, or any other LLM step via `steward.llm.call_llm()`

Setting `STEWARD_LLM_CLI=claude` switches **all three** atomically. Your
task's deep research engine starts using Claude Code; the reflector's fix
agents start using Claude Code; new task spawns use Claude Code. No code
changes anywhere.

| Provider | Set as default |
|----------|----------------|
| **Codex** (OpenAI) | `export STEWARD_LLM_CLI=codex` |
| **Claude Code** (Anthropic) | `export STEWARD_LLM_CLI=claude` |
| **Custom** (Gemini, OpenCode, ...) | `export STEWARD_LLM_CLI=my-cli` + template |

Auto-detected if not set. Per-task override in `charter.yaml`:
```yaml
execution:
  agent: "claude"    # this task always uses Claude Code, even if global default is codex
```

Custom CLI template via `STEWARD_LLM_CMD_TEMPLATE` env var — see
[docs/architecture.md](docs/architecture.md) for details.

**Why this matters**: as CLI tools and pricing change month-to-month, you
can switch providers without rewriting your tasks. The same `paper_reader`
that runs on Codex today can run on Claude Code Max tomorrow with one
environment variable.

---

## Why not just use Claude Code or Codex directly?

Claude Code, Codex, and Gemini CLI are **interactive tools** — you sit at
the terminal, type a request, watch it work, type the next request.
Steward turns them into **delegated background workers**:

- **You set up tasks once.** They run on a schedule (hourly/daily/weekly)
  without you opening the terminal.
- **You read a daily digest**, not a transcript. One email summarizing
  what every task produced today, with action items extracted.
- **It self-heals across days.** When a task breaks, the reflector
  detects it, spawns a fix agent, re-runs the task, and verifies the
  output before declaring it resolved. You only see the failure if the
  reflector can't resolve it.
- **It uses Claude Code / Codex under the hood.** Steward isn't a
  replacement — it's a thin layer that delegates to the CLI agent you
  already have.

If you only need to run something once, use Claude Code directly. If
you need to run it every morning at 5 AM and only hear about it when
something is wrong, that's what Steward is for.

---

## How It Differs

Steward is not another always-on agent or cron wrapper. The key difference:

**Progressive workflow hardening.** Tasks start in exploratory agent mode
(expensive, flexible), then promote to direct-mode scripts (cheap, editable)
as they stabilize. The `steward-promote` command analyzes execution history
and generates the script for you.

| | Always-on agents | Steward |
|---|---|---|
| **Cost** | Full LLM every cycle | LLM only where needed |
| **Control** | Agent decides everything | You see exactly what runs |
| **Editability** | Modify prompts and hope | Edit Python directly |
| **Failure** | Retry the whole agent | Self-healing fixes the specific step |
| **Attention** | Watch it constantly | Read the daily digest |

---

## Workflow Promotion

```bash
# Analyze a task's execution history
steward-promote daily_planner --last 10

# Produces:
#   promotion_report.md    — what's scriptable vs. needs LLM
#   run.py.generated       — candidate direct-mode script
#   charter.promoted.yaml  — updated config

# Or let it happen via email: reply to the daily digest with
#   approve daily_planner
#   reject daily_planner: still need LLM for inbox parsing
#   pause promotion daily_planner
```

See [docs/workflow-promotion.md](docs/workflow-promotion.md) for the full guide.

---

## Task Configuration

Each task has a `charter.yaml`:

```yaml
task_id: "my_task"
name: "Human-readable name"

schedule:
  frequency: "daily"          # hourly | daily | weekly
  max_runtime_minutes: 60

execution:
  agent: "direct"             # codex | claude | direct | custom
  entrypoint: "python run.py"

report:
  digest: true
  own_email:
    enabled: true
    prefix: "[MY-TASK]"
```

### Execution modes

| Mode | How it works | LLM cost |
|------|-------------|----------|
| **Agent** | LLM agent runs the whole workflow | High |
| **Direct** | Python script drives, calls LLM selectively | Low |
| **Hybrid** | Direct mode + automatic self-healing on crash | Low (high on failure) |

---

## Communication

Reports delivered via **email** (SMTP/Gmail). Reply to steer task behavior —
corrections, new priorities, pause/resume. The email layer is modular
(`steward/comm/email.py`).

---

## Documentation

| Doc | What |
|-----|------|
| [guide.md](guide.md) | Full setup and usage guide |
| [docs/architecture.md](docs/architecture.md) | System architecture and execution modes |
| [docs/workflow-promotion.md](docs/workflow-promotion.md) | Agent → direct promotion guide |
| [docs/agent-setup.md](docs/agent-setup.md) | AI agent bootstrapping instructions |
| [docs/comparison.md](docs/comparison.md) | vs OpenClaw, autoresearch, Airflow |
| [ROADMAP.md](ROADMAP.md) | What works now and what's next |

---

## Requirements

- Python 3.10+
- A CLI coding agent: [Codex](https://github.com/openai/codex), [Claude Code](https://claude.ai/code), or any custom CLI
- `pyyaml>=6.0`

```bash
pip install -e /path/to/steward/
# Installs: steward, steward-init, steward-status, steward-promote, steward-add-task
```

---

## License

MIT
