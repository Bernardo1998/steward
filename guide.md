# Charter Worker Setup Guide — Linux Server

This guide walks through installing charter-worker on a remote Linux server,
configuring it, and creating an experiment task that runs autonomously after
you log off. Written so that a human or an AI agent can follow it end-to-end.

## Prerequisites

- Linux server with SSH access (Ubuntu/Debian assumed, adapt for others)
- Python 3.10+
- Node.js 18+ (for `codex` CLI)
- A Gmail account for the bot (separate from your personal email)
- An OpenAI API key (for codex) OR Anthropic API key (for claude)
- `git`, `tmux` or `screen`

---

## 1. Install system dependencies

```bash
# Python, pip, git, tmux
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git tmux

# Node.js 18+ (for codex CLI)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Verify
python3 --version   # >= 3.10
node --version       # >= 18
```

---

## 2. Install the AI agent CLI

Pick one (or both). The orchestrator supports `codex` and `claude` as agent backends.

### Option A: OpenAI Codex CLI (recommended for experiments)

```bash
npm install -g @openai/codex
export OPENAI_API_KEY="sk-..."   # add to ~/.bashrc or ~/.profile
codex --version
```

### Option B: Anthropic Claude Code

```bash
npm install -g @anthropic-ai/claude-code
export ANTHROPIC_API_KEY="sk-ant-..."   # add to ~/.bashrc or ~/.profile
claude --version
```

Make the API key persistent:

```bash
echo 'export OPENAI_API_KEY="sk-..."' >> ~/.bashrc
# or
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
source ~/.bashrc
```

---

## 3. Clone and install charter-worker

```bash
cd ~
git clone https://github.com/Bernardo1998/charter-worker.git
pip install -e ~/charter-worker/

# Verify
charter-orchestrator --help
```

This installs:
- `charter-orchestrator` CLI (the main loop)
- `charter_worker` Python package (email, research, guardrails)
- Dependencies: `pyyaml`
- Optional: `pip install markdown weasyprint` (for HTML/PDF emails)

---

## 4. Create an instance directory

An "instance" is a directory where your tasks, state, and outputs live. It is
separate from the charter-worker code so you can have multiple instances.

```bash
# Create instance
mkdir -p ~/my-instance/tasks/_shared/state
mkdir -p ~/my-instance/daily_summaries

# Point charter-worker at it
export CHARTER_INSTANCE_ROOT="$HOME/my-instance"
```

---

## 5. Configure email

The system sends you daily reports and task emails. You reply to give feedback.

```bash
cp ~/charter-worker/templates/email_config.example.yaml ~/my-instance/email_config.yaml
```

Edit `~/my-instance/email_config.yaml`:

```yaml
sender:
  address: "yourbotaccount@gmail.com"      # dedicated Gmail for the bot
  app_password: "xxxx xxxx xxxx xxxx"      # Gmail App Password (NOT login password)
  smtp_server: "smtp.gmail.com"
  smtp_port: 587

recipient_allowlist:
  - "your-real-email@gmail.com"            # only these addresses can receive

rate_limit:
  max_sends_per_day: 20
  cooldown_seconds: 30

enabled: true
```

### Gmail App Password setup

1. Log into the bot's Gmail account
2. Go to Google Account > Security > 2-Step Verification > Enable
3. Go to Google Account > Security > App Passwords
4. Create one for "Mail" — copy the 16-character password
5. Paste it as `app_password` above

**Security**: `email_config.yaml` contains credentials — never commit it to git.

---

## 6. Create the task registry

```bash
cat > ~/my-instance/tasks/registry.yaml << 'EOF'
version: 2

tasks:
  - id: "my_experiment"
    enabled: true
    path: "tasks/my_experiment"
EOF
```

---

## 7. Create an experiment task

An experiment task is a self-contained folder with:
- `charter.yaml` — orchestrator config (schedule, agent, constraints)
- `task.md` — instructions for the AI agent (what to do, how to report)
- `state/experiment_state.json` — persistent state across cycles

### 7a. Directory structure

```bash
mkdir -p ~/my-instance/tasks/my_experiment/state
```

### 7b. charter.yaml

```bash
cat > ~/my-instance/tasks/my_experiment/charter.yaml << 'EOF'
task_id: "my_experiment"
name: "My Experiment"

schedule:
  frequency: "daily"
  max_runtime_minutes: 360       # kill after 6 hours

execution:
  agent: "codex"                 # or "claude" or "direct"
  sandbox: "none"                # "none" = full filesystem access
  auto_resume: true              # resume on context exhaustion
  max_resume_iterations: 5       # max resume attempts per cycle

constraints:
  file_exists:
    - "email_config.yaml"
  command_available:
    - "codex"                    # or "claude"

context:
  description: >
    You are a research experiment runner. Your workspace is at
    /path/to/workspace/. Read charter.yaml and task.md for instructions.
    Execute one cycle of the experiment, then exit.
  instructions_file: "task.md"

report:
  digest: true
  own_email:
    enabled: true
    prefix: "[MY-EXP]"
    on: "always"
EOF
```

**Execution modes:**
- `agent: "codex"` — orchestrator spawns `codex exec` with the prompt
- `agent: "claude"` — orchestrator spawns `claude -p` with the prompt
- `agent: "direct"` — orchestrator runs `entrypoint` directly (e.g., `python run.py`).
  Use this when you have a self-contained script that calls LLMs internally.

### 7c. task.md

This is the instruction file the AI agent reads each cycle. Copy and customize
the template:

```bash
cp ~/charter-worker/templates/experiment_task/task.md \
   ~/my-instance/tasks/my_experiment/task.md
```

Edit the top section ("Immutable Goal") with your experiment details:
- Research question
- Methods to compare
- Evaluation metrics
- Datasets
- Workspace path
- Success thresholds

The rest of the template defines the agent's behavior: state machine, autonomy
ladder, budget constraints, email discipline, and verification phase. These are
battle-tested defaults — modify only if you have specific needs.

**Key behavior**: The agent proposes a plan on first run and waits for your
email approval. After that, it runs autonomously — grinding through steps,
self-critiquing results, and sending you ONE daily report email. Your silence
means approval. Reply to steer.

### 7d. Initial state

```bash
cat > ~/my-instance/tasks/my_experiment/state/experiment_state.json << 'EOF'
{
  "status": "needs_plan",
  "current_step": 0,
  "last_email_date": "",
  "daily_email_sent_date": "",
  "accumulated_report": {
    "progress": [],
    "decisions": [],
    "critique": [],
    "questions": []
  },
  "notes": "",
  "completed_steps": [],
  "pending_steps": [],
  "failed_steps": [],
  "budget_used": {"gpu_minutes": 0, "api_tokens": 0, "emails_sent": 0}
}
EOF
```

---

## 8. Create the runner script

```bash
cat > ~/my-instance/run.sh << 'SCRIPT'
#!/bin/bash
set -euo pipefail

export CHARTER_INSTANCE_ROOT="$HOME/my-instance"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

# Load nvm if installed (for codex)
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd "$CHARTER_INSTANCE_ROOT"

DATE=$(date +%Y-%m-%d)
LOG_DIR="daily_summaries/${DATE}"
mkdir -p "$LOG_DIR"

echo "[$(date)] Starting orchestrator cycle..." >> "${LOG_DIR}/run.log"
charter-orchestrator >> "${LOG_DIR}/run.log" 2>&1
echo "[$(date)] Orchestrator cycle complete." >> "${LOG_DIR}/run.log"
SCRIPT

chmod +x ~/my-instance/run.sh
```

---

## 9. Run it (persists after SSH logout)

The orchestrator runs one cycle then exits. You need either **cron** (hourly
auto-runs) or **tmux** (manual runs that survive logout), or both.

### Option A: Cron (recommended — fully autonomous)

```bash
# Edit crontab
crontab -e

# Add hourly execution (runs at minute 0 of every hour):
0 * * * * /bin/bash -l ~/my-instance/run.sh
```

This runs the orchestrator every hour. The orchestrator checks each task's
schedule and only spawns tasks that are due. Daily tasks run once per day.
Long-running tasks continue in the background between cycles.

**Verify cron is working:**

```bash
# After the next hour mark, check:
cat ~/my-instance/daily_summaries/$(date +%Y-%m-%d)/run.log
```

### Option B: tmux (for manual/one-off runs)

```bash
# Start a tmux session that survives SSH disconnect
tmux new-session -d -s charter

# Run the orchestrator inside it
tmux send-keys -t charter '~/my-instance/run.sh' Enter

# Detach (Ctrl-B, then D) or just close SSH — it keeps running

# Re-attach later:
tmux attach -t charter
```

### Option C: Both (recommended)

Use cron for the hourly loop. Use tmux when you want to watch live output
or run manual commands:

```bash
# Cron handles hourly runs (set up as in Option A)

# For live monitoring:
tmux new-session -s monitor
tail -f ~/my-instance/daily_summaries/$(date +%Y-%m-%d)/run.log
```

---

## 10. Verify the setup

### Dry run (shows what would happen without spawning anything)

```bash
export CHARTER_INSTANCE_ROOT="$HOME/my-instance"
charter-orchestrator --dry-run
```

Expected output:
```
[orch] Orchestrator starting — 2026-03-21 14:30
[orch] Instance root: /home/user/my-instance
  [DRY RUN] Would spawn codex for my_experiment
```

### Real first run

```bash
~/my-instance/run.sh
```

The agent will:
1. Read `charter.yaml` and `task.md`
2. Draft an experiment plan
3. Email you the plan for approval
4. Set state to `awaiting_feedback` and exit
5. On the next cycle (after you reply), start implementing

### Check output

```bash
# Orchestrator log
cat ~/my-instance/daily_summaries/$(date +%Y-%m-%d)/run.log

# Task summary (after task completes)
cat ~/my-instance/daily_summaries/$(date +%Y-%m-%d)/tasks/my_experiment/summary.md

# Task state
cat ~/my-instance/tasks/my_experiment/state/experiment_state.json
```

---

## 11. How the lifecycle works

```
┌─────────────────────────────────────────────────────────────┐
│ CRON (hourly)                                               │
│   └─ charter-orchestrator                                   │
│        ├─ load state + registry                             │
│        ├─ check for crashed tasks from last cycle           │
│        │    └─ send failure email (dedup: 1/day)            │
│        ├─ for each enabled task:                            │
│        │    ├─ is_due(schedule)? → skip if not              │
│        │    ├─ preflight(constraints)? → skip if fail       │
│        │    ├─ is_locked()? → skip if running               │
│        │    └─ spawn agent                                  │
│        ├─ wait for short tasks (≤15 min)                    │
│        ├─ retry crashed daily tasks (up to 2/day)           │
│        ├─ completeness check at 5 AM                        │
│        ├─ daily digest email at 7 AM                        │
│        └─ save state                                        │
│                                                             │
│ SPAWNED TASK (runs independently)                           │
│   └─ codex exec / claude -p / python run.py                │
│        ├─ reads charter.yaml + task.md                      │
│        ├─ reads state/experiment_state.json                 │
│        ├─ executes steps (may take hours)                   │
│        ├─ sends daily report email                          │
│        ├─ writes summary.md + summary.json                  │
│        └─ saves updated state                               │
│                                                             │
│ YOU (async, via email)                                      │
│   ├─ receive daily report email each morning                │
│   ├─ reply to steer (or stay silent = approval)             │
│   └─ receive daily digest at 7 AM (all tasks combined)      │
└─────────────────────────────────────────────────────────────┘
```

---

## 12. Adding more tasks

Create a new task folder and register it:

```bash
# 1. Create task
mkdir -p ~/my-instance/tasks/another_task/state
# ... create charter.yaml, task.md, experiment_state.json (same pattern as above)

# 2. Register in registry.yaml
cat >> ~/my-instance/tasks/registry.yaml << 'EOF'

  - id: "another_task"
    enabled: true
    path: "tasks/another_task"
EOF
```

### Schedule options

In `charter.yaml`:

```yaml
schedule:
  frequency: "daily"             # once per day
  run_hour: 2                    # don't start before 2 AM (optional)
  max_runtime_minutes: 360

# Or:
schedule:
  frequency: "hourly"            # every orchestrator cycle
  max_runtime_minutes: 45

# Or:
schedule:
  frequency: "weekly"
  run_day: "Monday"              # only on Mondays
  max_runtime_minutes: 120
```

### Staggering tasks to avoid OOM

If running multiple heavy tasks, stagger them with `run_hour`:

```yaml
# Task A: starts at 1 AM
schedule:
  frequency: "daily"
  run_hour: 1

# Task B: starts at 4 AM (after A likely finishes)
schedule:
  frequency: "daily"
  run_hour: 4
```

---

## 13. Using "direct" execution mode

For tasks with their own `run.py` that call LLMs internally (instead of being
wrapped in a codex/claude session):

```yaml
# charter.yaml
execution:
  agent: "direct"
  entrypoint: "python run.py"
  sandbox: "none"
```

The orchestrator runs `bash -lc "python run.py"` in the task directory. Your
`run.py` must handle everything: reading state, calling LLMs, writing summaries,
sending emails.

### Key difference from agent mode

In **agent mode** (`agent: "codex"` or `"claude"`), the LLM agent is the workflow
driver — it reads `task.md` and autonomously decides what to do each cycle.

In **direct mode** (`agent: "direct"`), your Python script is the driver. It
controls the exact sequence of operations. The LLM is only called for specific
steps that genuinely need reasoning (synthesis, evaluation, summarization).
Everything else is deterministic Python.

```
Agent mode:   LLM reads task.md → decides what to do → runs tools → writes output
Direct mode:  Python script → step1() → call_llm() → step3() → save_state()
                                            ↑
                               only this step uses the LLM
```

### Making LLM calls from direct-mode tasks

Use `call_llm_json()` from `charter_worker.proactive.llm` for structured output,
or `call_llm()` for raw text. These automatically use whichever CLI provider is
configured (codex, claude, or custom — see README for provider configuration).

```python
#!/usr/bin/env python3
"""Example direct-mode task with selective LLM calls."""

import json, os, sys, time
from pathlib import Path
from datetime import datetime

# Setup paths
TASK_DIR = Path(__file__).resolve().parent
REPO_ROOT = TASK_DIR.parent.parent
os.environ.setdefault("CHARTER_INSTANCE_ROOT", str(REPO_ROOT))

from charter_worker.proactive.llm import call_llm_json

def load_state():
    """Pure Python — no LLM needed."""
    state_file = TASK_DIR / "state" / "state.json"
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"last_run": None, "results": []}

def gather_data():
    """Pure Python — read files, call APIs, etc."""
    return {"items": ["item1", "item2", "item3"]}

def analyze_with_llm(data, state):
    """This step genuinely needs LLM reasoning."""
    prompt = f"""Analyze these items and suggest priorities.
Items: {json.dumps(data['items'])}
Previous results: {json.dumps(state.get('results', [])[-3:])}

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "priorities": ["highest priority item", ...],
  "rationale": "why this ordering"
}}"""
    try:
        return call_llm_json(prompt, timeout=120)
    except Exception as e:
        print(f"LLM analysis failed: {e}", file=sys.stderr)
        return {"priorities": data["items"], "rationale": "LLM unavailable, using original order"}

def save_state(state):
    """Pure Python — no LLM needed."""
    state_dir = TASK_DIR / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(state, indent=2))

def write_summary(result, started_at):
    """Write summary files for the orchestrator to collect."""
    summary_dir = Path(os.environ.get("CHARTER_SUMMARY_DIR",
        f"{REPO_ROOT}/daily_summaries/{datetime.now():%Y-%m-%d}/tasks/my_task"))
    summary_dir.mkdir(parents=True, exist_ok=True)

    (summary_dir / "summary.md").write_text(
        f"# My Task\\n\\n## Priorities\\n" +
        "\\n".join(f"- {p}" for p in result["priorities"])
    )
    (summary_dir / "summary.json").write_text(json.dumps({
        "task_id": "my_task",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "status": "success",
        "tldr": result["priorities"][:2],
        "metadata": {"duration_s": round(time.time() - started_at, 1)},
    }, indent=2))

if __name__ == "__main__":
    started = time.time()
    state = load_state()                    # pure Python
    data = gather_data()                    # pure Python
    result = analyze_with_llm(data, state)  # ONE LLM call
    state["results"].append(result)
    state["last_run"] = datetime.now().isoformat()
    save_state(state)                       # pure Python
    write_summary(result, started)          # pure Python
```

### Reusable components for direct-mode tasks

`charter_worker.proactive.*` modules provide pre-built phases:
- `phase_research.research()` — web search + deep research
- `phase_synthesize.synthesize()` — extract claims, update hypothesis
- `phase_feedback.send_ltt_email()` — compose and send report
- `comm.email.send_email()` — raw email send
- `proactive.gmail_reader.fetch_ltt_replies()` — read email replies

All of these use `call_llm_json()` internally, so they automatically respect
the configured CLI provider.

---

## 14. Monitoring and troubleshooting

### Live log

```bash
tail -f ~/my-instance/daily_summaries/$(date +%Y-%m-%d)/run.log
```

### Task-specific log

```bash
tail -f ~/my-instance/tasks/my_experiment/logs/cycle_$(date +%Y-%m-%d).log
```

### Check orchestrator state

```bash
python3 -m json.tool ~/my-instance/orchestrator_state.json
```

### Check task state

```bash
python3 -m json.tool ~/my-instance/tasks/my_experiment/state/experiment_state.json
```

### Force-run a task (ignoring schedule)

```bash
export CHARTER_INSTANCE_ROOT="$HOME/my-instance"
charter-orchestrator --force my_experiment
```

### Clear a stale lock

If a task is stuck "locked" after a crash:

```bash
rm ~/my-instance/tasks/my_experiment/.lock
```

### Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `codex: command not found` | Node/npm not in cron PATH | Add to `run.sh`: `export NVM_DIR=...` and source nvm |
| `No registry at ...` | Wrong `CHARTER_INSTANCE_ROOT` | Check the export in `run.sh` |
| `Email config not found` | Missing `email_config.yaml` | Copy from template, fill in credentials |
| `rate_limited` on email | Too many emails today | Wait; check `rate_limit` in email config |
| Tasks crash with OOM | Too many concurrent agents | Stagger with `run_hour`; reduce deep_research `max_workers` |
| Task never runs | Schedule says not due | Check `run_hour`, frequency; use `--force` to test |
| Cron not firing | Cron service not running | `sudo systemctl enable cron && sudo systemctl start cron` |

---

## 15. File reference

```
~/charter-worker/                    # Framework (git-tracked)
  orchestrator.py                    # Main loop (schedule, spawn, self-healing, digest)
  preflight.py                       # Constraint checker
  agent_loop.sh                      # Auto-resume wrapper
  charter_worker/                    # Python package
    proactive/llm.py                 #   CLI-agnostic LLM abstraction (codex/claude/custom)
    proactive/reflection/            #   Self-reflection system (daily health analysis)
    proactive/phase_*.py             #   5-phase proactive research cycle
    proactive/guardrails.py          #   G1-G12 guardrails
    executor/agent.py                #   CLI agent session launcher
    comm/email.py                    #   Email sender
    research/                        #   Deep research engine
    utils/helpers.py                 #   Utility functions

~/my-instance/                       # Your instance (git-track this too)
  email_config.yaml                  # Credentials (gitignore this!)
  orchestrator_state.json            # Auto-managed state
  run.sh                             # Runner script
  tasks/
    registry.yaml                    # Task list
    _shared/state/                   # Shared state (email logs)
    my_experiment/
      charter.yaml                   # Task config
      task.md                        # Agent instructions
      state/experiment_state.json    # Task state
      logs/cycle_YYYY-MM-DD.log      # Auto-created per run
  daily_summaries/
    YYYY-MM-DD/
      run.log                        # Orchestrator log
      daily_digest.md                # Combined summary
      tasks/my_experiment/
        summary.md                   # Task summary
        summary.json                 # Structured summary
```

---

## Quick-start checklist

```
[ ] Python 3.10+ installed
[ ] Node.js 18+ installed
[ ] CLI agent installed: codex, claude, or custom (set CHARTER_LLM_CLI if not codex)
[ ] charter-worker cloned and pip installed
[ ] Instance directory created
[ ] email_config.yaml configured with Gmail App Password
[ ] registry.yaml created with task entry
[ ] Task folder created (charter.yaml, task.md, state/experiment_state.json)
[ ] run.sh created and chmod +x
[ ] Dry run passes: charter-orchestrator --dry-run
[ ] First real run: task sends plan email
[ ] Cron job added: 0 * * * * /bin/bash -l ~/my-instance/run.sh
[ ] SSH logout test: task keeps running after disconnect
```
