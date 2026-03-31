# charter-worker

**An autonomous agent orchestrator that runs your recurring tasks, heals itself when things break, and improves over time — without human intervention.**

Set up your daily/weekly tasks once, point them at any CLI coding agent (Codex, Claude Code, Gemini Code, OpenCode, ...), and let the orchestrator run them on a cron loop forever. It schedules, spawns, collects results, emails you a digest, and when something crashes, it diagnoses the failure, applies a fix, and retries — all before you wake up.

![charter-worker system overview](docs/charter_workflow.png)

---

## Quick Start

### Option A: Let your AI agent set it up

Point your AI agent at [`docs/agent-setup.md`](docs/agent-setup.md) — it
contains step-by-step instructions to bootstrap a working instance automatically.

### Option B: Zero-dependency first run

```bash
pip install -e /path/to/charter-worker/
charter-init ./my-tasks
cd my-tasks
charter-orchestrator --force hello_world
```

This creates an instance with the `hello_world` template — a pure Python task
that counts files and writes a summary. No Codex, no Claude, no API keys.

### Option C: Agent-backed task

```bash
charter-init ./my-tasks --template ltt_thinker
cd my-tasks
# Requires: a CLI agent installed (codex, claude, etc.)
charter-orchestrator --force ltt_thinker
```

### Automate (cron)

```bash
# Run every hour — the orchestrator handles scheduling internally
0 * * * * CHARTER_INSTANCE_ROOT=/path/to/my-tasks charter-orchestrator >> cron.log 2>&1
```

---

## CLI Agent Support

Charter-worker is **CLI-agent agnostic**. Any coding agent that accepts a prompt
and can read/write files will work — the same task runs on Codex, Claude Code,
Gemini Code, OpenCode, or your own custom CLI without code changes.

### Built-in providers

| Provider | Binary | Set as default |
|----------|--------|----------------|
| **Codex** (OpenAI) | `codex` | `export CHARTER_LLM_CLI=codex` |
| **Claude Code** (Anthropic) | `claude` | `export CHARTER_LLM_CLI=claude` |

Auto-detected if not set (prefers codex, falls back to claude).

Per-task override in `charter.yaml`:
```yaml
execution:
  agent: "claude"    # this task always uses Claude Code
```

### Custom providers (Gemini Code, OpenCode, etc.)

```bash
export CHARTER_LLM_CLI=gemini-code
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

The template defines how to build commands for two modes: `read_only` (analysis)
and `write` (code changes). This abstraction is used everywhere — task spawning,
self-healing, reflection, and all internal LLM calls.

---

## Task Configuration

Each task lives in its own folder with a `charter.yaml`:

```yaml
task_id: "my_task"
name: "Human-readable name"

schedule:
  frequency: "daily"          # "hourly" | "daily" | "weekly"
  run_day: "Monday"           # weekly only
  run_hour: 6                 # daily only — skip before this hour
  max_runtime_minutes: 60

execution:
  agent: "direct"             # "codex" | "claude" | "direct" | custom
  entrypoint: "python run.py" # required for "direct" mode

constraints:                  # all optional
  command_available: ["codex"]
  file_exists: ["email_config.yaml"]

report:
  digest: true
  own_email:
    enabled: true
    prefix: "[MY-TASK]"
    on: "always"              # "always" | "on_change" | "on_error"
```

### Execution modes

| Mode | `execution.agent` | How it works | LLM cost |
|------|-------------------|--------------|----------|
| **Agent** | `codex`, `claude`, etc. | LLM agent reads `task.md` and runs the whole workflow | High |
| **Direct** | `direct` | Python script drives; calls LLM selectively via `call_llm_json()` | Low |
| **Hybrid** | `direct` + self-healing | Direct mode, but crashes trigger automatic diagnosis and repair | Low (high on failure) |

Tasks naturally promote from agent to direct as they stabilize.
See [docs/workflow-promotion.md](docs/workflow-promotion.md).

### Templates

```bash
charter-init --list-templates

charter-init ./my-tasks --template hello_world       # direct mode, zero deps
charter-init ./my-tasks --template ltt_thinker       # agent mode, research
charter-init ./my-tasks --template experiment_task   # agent mode, experiments
```

---

## Communication

Reports are delivered via **email** (SMTP/Gmail). Each task can send its own
email, and the orchestrator sends a daily digest combining all results with a
system health report.

Users **reply to emails** to steer task behavior — corrections, new priorities,
pause/resume commands. The next cycle reads the reply and adjusts.

The email layer is modular (`charter_worker/comm/email.py`). Adding Slack,
Telegram, or webhook delivery requires implementing the same `send` interface.

---

## Documentation

| Doc | What |
|-----|------|
| [guide.md](guide.md) | Full setup and usage guide (direct mode examples, monitoring, troubleshooting) |
| [docs/architecture.md](docs/architecture.md) | System architecture, execution modes, self-healing details |
| [docs/workflow-promotion.md](docs/workflow-promotion.md) | Agent → direct promotion guide |
| [docs/agent-setup.md](docs/agent-setup.md) | AI agent bootstrapping instructions |
| [docs/comparison.md](docs/comparison.md) | vs OpenClaw, autoresearch, Airflow |
| [ROADMAP.md](ROADMAP.md) | What works now and what's next |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |

---

## Requirements

- Python 3.10+
- A CLI coding agent: [Codex](https://github.com/openai/codex), [Claude Code](https://claude.ai/code), or any custom CLI
- `pyyaml>=6.0`
- Optional: `markdown>=3.4`, `weasyprint>=60.0` (PDF email attachments)

```bash
pip install -e /path/to/charter-worker/

# Installs: charter-orchestrator, charter-init, charter-status
```

---

## License

MIT
