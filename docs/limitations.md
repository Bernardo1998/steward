# Limitations and Honest Gaps

Charter-worker is a research preview. It works well for its author's daily
workflow but has clear limitations you should know before adopting it.

## Current limitations

### Single-user design
The system assumes one human operator. There is no multi-user auth, shared
state, or collaborative editing. The email feedback loop is 1:1.

### No web UI or dashboard
Status is available via `charter-status` (CLI) and email digests. There is
no browser-based dashboard. A minimal `status.md` output exists but is not
auto-served.

### No DAG or task dependencies
Tasks run independently. If task B depends on task A's output, you must
handle that in task B's code (check if the file exists, skip if not).

### No automatic workflow crystallization
The [workflow promotion](workflow-promotion.md) concept is documented and
the infrastructure exists (`agent: "direct"` mode + self-healing), but
there is no automatic detection of "this task is stable enough to promote."
That is a research direction, not a shipped feature.

### Agent dependency
Exploratory tasks require a CLI coding agent installed and authenticated.
Built-in support for [Codex CLI](https://github.com/openai/codex) and
[Claude Code](https://claude.ai/code). Custom CLIs (Gemini Code, OpenCode, etc.)
are supported via `CHARTER_LLM_CMD_TEMPLATE` — see README for configuration.
The `hello_world` template avoids this dependency entirely.

### Email-only reporting
Reports are sent via SMTP (Gmail). There is no Slack, webhook, or API
integration for notifications.

### Limited testing
The test suite covers the bootstrap and status commands. The orchestrator,
research engine, and proactive loop are tested via daily dogfooding but do
not have unit tests yet.

## Research gaps (from docs/theory.md)

### When to decompose
The organizational theory framework predicts when single-agent should beat
multi-agent (and vice versa), but the empirical validation is not yet
complete. The smoke test is in progress.

### Coordination cost measurement
The theory says to measure coordination costs and compare them to the
benefits of decomposition. We don't yet have a reliable, framework-neutral
way to measure these costs from execution traces.

### Routine detection
The "amortized autonomy" thesis claims that stable work should be
crystallized into cheap workflows. Detecting stability and generating
the workflow automatically is future work.

## What works well today

- Hourly/daily/weekly task scheduling with lock management
- CLI-agnostic provider abstraction (codex, claude, custom CLIs)
- Direct-entrypoint tasks (zero LLM cost for routine work)
- Reactive self-healing on crash (diagnostic agent → fix → retry)
- Proactive self-reflection (daily multi-day failure analysis, durable fixes, engagement tracking)
- Email feedback loop (reply to steer task behavior)
- Proactive research with 10 guardrails (weeks of autonomous operation)
- Experiment dispatch with budget tracking
- Deep research engine (fan-out/fan-in with web search)
