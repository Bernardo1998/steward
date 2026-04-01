# How steward compares

## vs OpenClaw / persistent autonomous agents

OpenClaw and similar systems emphasize persistent autonomous iteration: the
agent runs continuously, deciding what to do, executing, and looping.

Charter-worker takes a different approach:

| | OpenClaw-style | Charter-worker |
|---|---|---|
| **Execution** | Continuous autonomous loop | Scheduled cycles (hourly/daily/weekly) |
| **Control** | Agent decides everything | Charters, guardrails, human feedback loops |
| **Cost model** | Constant high LLM usage | Amortized: routine work runs as cheap scripts |
| **Recovery** | Agent self-corrects (or doesn't) | Orchestrator diagnoses, fixes, retries |
| **Observability** | Logs | Structured summaries, email reports, status surface |

The key difference is **amortized autonomy**: repeated work should crystallize
into cheap explicit workflows, not stay in permanent high-cost agent mode.

## vs autoresearch / FARS / ARIS / Elicit

These are research-specific pipelines with hard-separated stages:
search → read → summarize → (maybe) write.

Charter-worker differs in several ways:

| | Research pipelines | Charter-worker |
|---|---|---|
| **Scope** | Research only | Any recurring task |
| **Pipeline** | Fixed stages (search → read → write) | Interleaved: search, act, experiment, escalate |
| **Experiments** | Not supported | Built-in experiment dispatch + result parsing |
| **Feedback** | One-shot or manual iteration | Email feedback loop, reply-to-steer |
| **Self-healing** | None | Diagnostic agent on crash |
| **Long-horizon** | Single session | State persists across cycles with 10 guardrails |
| **Theory** | Ad-hoc pipeline design | Grounded in organizational theory (Coase, Simon, Galbraith) |

## vs general task runners (cron, Airflow, Prefect)

Charter-worker is not a workflow orchestrator in the traditional sense.
It does not have DAGs, task dependencies, or a web UI.

What it adds over plain cron:
- **Agent dispatch**: spawns LLM agents (codex, claude) as subprocesses
- **Lock management**: prevents double-spawning across hourly cron cycles
- **Preflight checks**: validates prerequisites before running
- **Self-healing**: diagnoses and fixes crashes automatically
- **Structured summaries**: every task produces `summary.json` + `summary.md`
- **Email digest**: collects all results into a single daily email
- **Workflow promotion**: tasks can graduate from expensive agent mode to cheap scripts

## What steward is NOT

- Not a web product or SaaS
- Not a replacement for Airflow/Prefect for production data pipelines
- Not a general-purpose AI assistant (it runs **your** recurring tasks)
- Not a multi-agent framework (it orchestrates tasks, not agent conversations)

## What it IS

A governed autonomy engine for recurring tasks, grounded in organizational
theory, designed for a single researcher or developer who wants their AI
agent to handle routine work reliably and cheaply.
