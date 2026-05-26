# Plan-runner setup guide (v3.1) — deploy on a fresh server

**Audience:** an agent (or human) with zero prior context on a fresh
Linux/WSL/macOS server. Assumes only that `git` and `python3` (>=3.10)
are installed. Nothing about steward, TimeManagement, or any prior
config is presumed to exist.

**You will:** clone steward, pip-install it, scaffold a fresh instance
directory with one plan_runner task, ask the user for the project
inputs, fill in `goal.md` + `plan.yaml`, wire a cron entry, and verify
one cycle runs cleanly. ~10 min of work plus whatever the user needs to
draft their goal and plan.

This guide is self-contained — do not search for prior conversation.
Execute the steps in order. **STOP at every "ASK THE USER" block.**

---

## 0. Glossary (read once)

- **steward** = the Python library at `https://github.com/Bernardo1998/steward`.
  Provides:
  - `steward.phases.plan_phases` (low-level cycle helpers)
  - `steward.plan_runner.PlanRunner` (the reusable cycle engine)
  - `steward-plan-runner` (pip console script for scaffolding)
- **instance dir** = the per-machine working directory. Holds:
  - `tasks/` — one folder per plan_runner project
  - `tasks.yaml` — registry of enabled tasks
  - `daily_summaries/` — ephemeral per-day cycle outputs (auto-created)
  - `email_config.yaml` — Gmail OAuth credentials (you set up once)
  - Whatever cron entry / shell script you use to fire it
- **task** = one plan_runner project: `tasks/<task_id>/` with
  `goal.md`, `plan.yaml`, `README.md`, `run.py`, and `state/`.
- **experiment_repo** = absolute path where step dispatch actually runs.
  Lives **outside** the instance dir, e.g. `/srv/myproject/` or
  `~/research/mything/`.

The agent in each cycle reads exactly 4 files:
`goal.md`, `state/current_step.md`, `state/inbox.md`, `state/recap.md`.

---

## 1. Install steward

```bash
# Clone steward to wherever you want. ~/steward is a fine default.
git clone https://github.com/Bernardo1998/steward.git ~/steward
cd ~/steward
# Default branch (`master`) carries v3.1 once it's been merged. If you
# want to pin to the in-flight feature branch instead, uncomment:
# git checkout plan-runner-compression

# Install editable. This puts the `steward-plan-runner` CLI on PATH and
# makes `from steward.plan_runner import PlanRunner` work everywhere.
python3 -m pip install -e .
```

Verify:

```bash
which steward-plan-runner
steward-plan-runner init --help | head
python3 -c "
from steward.plan_runner import PlanRunner
from steward.plan_runner.scaffolder import init_task, verify_task
from steward.phases.plan_phases import load_goal_md, load_plan, load_state_json
print('all v3.1 imports OK')
"
```

If `steward-plan-runner` is missing from PATH (some pip setups don't
add `~/.local/bin/`), use the module form instead:
`python3 -m steward.plan_runner.scaffolder init ...`.

**Codex CLI (skip if every step in your plan will use a literal
`run_command:`):**

```bash
which codex || npm i -g @openai/codex
codex --version
```

---

## 2. Choose an instance directory

Pick where this machine's tasks + state will live. **It is not the
experiment_repo.** Examples:

- `~/plan_runner/` — typical single-user setup
- `/srv/plan_runner/` — server-style
- `/path/to/some/existing/repo/` — if you want tasks tracked in git

```bash
INSTANCE_DIR="$HOME/plan_runner"
mkdir -p "$INSTANCE_DIR"
cd "$INSTANCE_DIR"
```

The scaffolder will create `tasks/`, `tasks.yaml`, and the per-task
folder inside `$INSTANCE_DIR` on first run. Nothing else is needed
upfront.

---

## 3. Set up email config (one-time, per instance)

The runner emails one cycle digest per task per cycle. Provide Gmail
OAuth credentials at `$INSTANCE_DIR/email_config.yaml`. If you already
have a working `email_config.yaml` from another steward instance, copy
it over and skip this section.

ASK THE USER for their Gmail OAuth credentials (or for an existing
`email_config.yaml` file path), then drop it at
`$INSTANCE_DIR/email_config.yaml`. The expected shape:

```yaml
gmail:
  client_id: "..."
  client_secret: "..."
  refresh_token: "..."
  sender_email: "..."
  recipient_email: "..."   # usually the same as sender
```

If the user doesn't have OAuth set up, point them at
`~/steward/templates/email_config.example.yaml` and Google's OAuth docs.
You can defer this step and still scaffold the task — the first cycle
will print "email send failed" but everything else works.

---

## 4. ASK THE USER — gather project inputs

STOP. Do not proceed past this section until you have written-down
answers to every question. Re-ask if the answer is vague.

### 4.1 Identity

1. **task_id** — short kebab-case identifier, unique. Example:
   `qd_synth_runner`.
2. **Email subject prefix** — short tag in brackets. Example: `[QD]`.
3. **experiment_repo** — absolute path where step dispatch runs.
   Confirm it exists, or that the user wants the runner to create it.

### 4.2 The locked constitution (goal.md)

4. **Goal** — one paragraph. Specific enough that a stranger could
   check whether a result meets it. *(Locked — the agent never edits.)*
5. **Things to show** — 2-6 concrete deliverables (numbers, figures,
   tables, artifacts).
6. **In-scope** — bullet list of things the work WILL do.
7. **Out-of-scope** — bullet list of things the work explicitly will
   NOT do (protects against scope creep).
8. **Success criteria** — bullet list of "done" tests. Concrete.
9. **Methodology phases** — for each phase (P0, P1, …), title +
   1-2 sentence description. These become stages in the plan.

### 4.3 Schedule + budgets

10. **Cadence** — `hourly` (active dev) or `daily` (low-touch).
    Cooldown defaults to 3h.
11. **Step timeout** — wall-clock backstop per step. Default 1800s.
12. **Budget per step / per cycle** — USD ceilings. Defaults 30 / 50.

### 4.4 The plan (plan.yaml)

13. **Stages** — group the methodology phases from (9) into approval
    chapters. Each stage gets its own approval gate. Example:
    `warmup → setup → method → control → eval`.
14. **Steps under each stage** — for each step:
    - `id` (kebab-case, e.g. `p0_setup_sanity`)
    - `type` (one of: `experiment`, `research`, `replicate`, `ablate`,
      `robust`, `citation_harden`, `failure_probe`, `gap_check`)
    - `description` (concrete enough that an LLM could act without
      further design input — if vague, the sub-agent will pause with
      `INTENT_NEEDED: <question>`)
    - `success_criteria.hard` — at least one line in this grammar:
      - `file_exists: <path-relative-to-experiment_repo>`
      - `metric: <path>::<dotted.key> <op> <value>`
        (op ∈ `==, !=, >, <, >=, <=, is number, is string`)
    - `success_criteria.soft.rubric` (optional but recommended) — free
      text the LLM judge evaluates against.
    - `depends_on` (optional list of earlier step ids).
    - `max_attempts` (default 3).
    - `run_command` (optional shell command). When present, dispatch
      is deterministic shell-exec instead of Codex.
15. **First step the runner should run** — usually a smoke step that
    just writes a tiny `results/<id>_results.json` to confirm the
    pipeline works before any real work.

Echo the gathered answers back as a numbered summary and ASK THE USER
to confirm before proceeding to §5.

---

## 5. Scaffold the task

```bash
cd "$INSTANCE_DIR"

steward-plan-runner init <task_id> \
    --experiment-repo "<absolute experiment_repo>" \
    --email-prefix "[<TAG>]" \
    --schedule hourly      # or daily

# Or, if PATH doesn't include the pip script dir:
python3 -m steward.plan_runner.scaffolder init <task_id> \
    --experiment-repo "<absolute experiment_repo>" \
    --email-prefix "[<TAG>]"
```

This creates:

```
$INSTANCE_DIR/
├── tasks.yaml                            # registry (created if missing)
└── tasks/
    └── <task_id>/
        ├── goal.md                       # placeholder — fill in
        ├── plan.yaml                     # placeholder — fill in
        ├── README.md
        ├── run.py                        # 10-line caller
        └── state/
            ├── state.json
            └── exploration_log.jsonl     # empty
```

Pass `--dry-run` to preview without writing. Pass `--force` to
overwrite an existing task folder.

---

## 6. Fill in `goal.md` and `plan.yaml`

Open `$INSTANCE_DIR/tasks/<task_id>/goal.md` and replace each
placeholder section with the user's answers from §4.2 / §4.3. Keep the
H2 section headers exactly as they appear (`## Goal`,
`## Things to show`, `## Scope`, `## Success criteria`,
`## Methodology`, `## Orchestrator`, `## Experiment config`) — the
parser keys off them.

Open `$INSTANCE_DIR/tasks/<task_id>/plan.yaml` and:

1. Replace the placeholder `title:` with a short headline.
2. Bump `version: 1` is already set; leave it. `approved_at` is
   pre-filled to the scaffolding time.
3. Replace the placeholder `stages:` block with the user's grouping
   from §4.4 q13.
4. Replace the placeholder `steps:` block with the user's steps from
   §4.4 q14. Every step id must appear in **exactly one** stage's
   `steps:` list.
5. Leave `deepening_added_by_runner: []` alone — the runner appends.

Common gotchas:
- A step id appears in two stages, or in zero stages.
- `success_criteria.hard` line is plain English instead of
  `file_exists:` or `metric:` grammar.
- `## Orchestrator` section missing `email_prefix:`.

---

## 7. Verify

```bash
steward-plan-runner verify "$INSTANCE_DIR/tasks/<task_id>"
# Or:  python3 -m steward.plan_runner.scaffolder verify "$INSTANCE_DIR/tasks/<task_id>"
```

You should see:

```
OK: <task_id> is ready for first cycle
    next action: plan_step <first_step_id>
    banner: **Stage progress.** Stage `<first_stage>` (0/N steps done) — running step `<first_step_id>`
```

If any assertion fails, the message tells you which file to fix.

---

## 8. Wire it into cron

Three patterns; pick what matches your machine.

### 8a. Linux/macOS cron

```bash
mkdir -p "$INSTANCE_DIR/tasks/<task_id>/logs"

crontab -e
# Add ONE of:

# Hourly (the runner self-throttles to 3h via cooldown_hours):
0 * * * *  cd "$INSTANCE_DIR" && /usr/bin/python3 tasks/<task_id>/run.py >> tasks/<task_id>/logs/cron_$(date +\%Y-\%m-\%d).log 2>&1

# Daily at 5 AM:
0 5 * * *  cd "$INSTANCE_DIR" && /usr/bin/python3 tasks/<task_id>/run.py >> tasks/<task_id>/logs/cron_$(date +\%Y-\%m-\%d).log 2>&1
```

A cron entry firing every minute is fine — most ticks short-circuit at
the cooldown check.

### 8b. systemd timer (modern Linux)

```ini
# /etc/systemd/system/plan-runner-<task_id>.service
[Unit]
Description=plan-runner <task_id>
[Service]
Type=oneshot
WorkingDirectory=<INSTANCE_DIR>
ExecStart=/usr/bin/python3 <INSTANCE_DIR>/tasks/<task_id>/run.py

# /etc/systemd/system/plan-runner-<task_id>.timer
[Unit]
Description=plan-runner <task_id> timer
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
```

`sudo systemctl enable --now plan-runner-<task_id>.timer`.

### 8c. Windows Task Scheduler (WSL)

Use Windows Task Scheduler to run:
`wsl.exe -- bash -lc 'cd <INSTANCE_DIR> && python3 tasks/<task_id>/run.py'`
on the schedule you want.

---

## 9. First real cycle

This will dispatch the first step and send a cycle email. Cooldown is
already satisfied because the task never ran before.

```bash
cd "$INSTANCE_DIR/tasks/<task_id>" && python3 run.py
```

Watch stderr for:

- `[<task_id>] plan_runner cycle — <date>` — task started.
- `Cycle 1 selected action: plan_step` — picked the user's first step.
- `Dispatching step '<id>' via Codex sub-agent ...` (or `via run_command`).
- `email: success` — cycle email landed.
- `complete in <N>s` — clean exit.

Post-cycle check:

```bash
cat "$INSTANCE_DIR/daily_summaries/$(date +%Y-%m-%d)/tasks/<task_id>/summary.md"
```

The cycle email's subject line should match the `[<TAG>]` you set.

---

## 10. Daily workflow (tell the user)

| Situation | What to do |
|---|---|
| Approve advancing to the next stage | Reply to the cycle email with `/approve <stage_id>` or `/approve next`. The legacy `/approve v<N>` form also works. |
| Redirect / replan | Edit `tasks/<task_id>/plan.yaml`, bump `version`, update `approved_at`. The runner picks it up next cycle. |
| Answer an `INTENT_NEEDED: ...` question | Reply with `/answer <text>`, OR add `/answer <text>` to `plan.yaml::steps[<id>].notes`. |
| Pause | `echo 'paused: true' > tasks/<task_id>/state/status.yaml`. |
| Inspect cycle state | `cat tasks/<task_id>/state/recap.md`. |
| See what the agent did | `tail -20 tasks/<task_id>/state/exploration_log.jsonl`. |

---

## 11. Failure modes

| Symptom | Cause / fix |
|---|---|
| `ImportError: steward` | You forgot `pip install -e .` in the steward repo, or you're in a different Python environment. Re-run §1. |
| `steward-plan-runner: command not found` | Pip script dir not on PATH. Use `python3 -m steward.plan_runner.scaffolder init ...` instead. |
| `goal.md not found` | The folder isn't a scaffolded task. Re-run §5. |
| `plan.yaml::stages references unknown step` | A `stages[].steps[]` id doesn't match any `plan.steps[].id`. |
| `not assigned to any stage` | Every plan step must appear in exactly one stage. |
| Cycle email never arrives | `email_config.yaml` missing or invalid. See §3. |
| `codex CLI not found on PATH` | `npm i -g @openai/codex`, then retry. |
| Cycle stops at `awaiting_stage_approval` immediately | The first stage's steps are already marked completed in `state.json`. Either hand-edit `state.json` to back-date them, or reply `/approve next` to the cycle email. (Won't happen on a freshly-scaffolded task.) |

---

## 12. When you're done

Print to the user:

> "`<task_id>` is live in `<INSTANCE_DIR>`. First cycle ran at
> <timestamp>. Stage `<first_stage>` is active. Next cycle fires per
> your cron schedule (or sooner if you trigger it manually). Reply
> `/approve next` to the cycle email when the first stage finishes."

Point them at `tasks/<task_id>/README.md` for the day-to-day cheat
sheet.
