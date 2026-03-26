# Contributing to Charter-Worker

Thanks for your interest! This project is in active development as part of a
research effort on organizational theory applied to LLM agent design.

## Adding a task

1. Copy a template from `templates/` to your instance's `tasks/` directory
2. Edit `charter.yaml` (schedule, execution mode, constraints)
3. Edit `definition.yaml` (goal, scope, success criteria)
4. Write your `run.py` or `task.md` entry point
5. Register in `tasks/registry.yaml`
6. Run: `charter-orchestrator --force <task_id>`

See `templates/hello_world/` for a minimal working example.

## Project structure

```
charter_worker/
  proactive/    # Long-term research cycle (5-phase: context → research → synthesize → report → speculate)
  research/     # Deep research engine (planner → workers → aggregator → reviewer)
  executor/     # Experiment execution (auto-resume, step tracking)
  comm/         # Email composition, digest, Gmail IMAP reader
  core/         # Shared types and utilities
  utils/        # Helpers
```

## Code style

- Python 3.10+
- No strict formatter enforced yet — match surrounding code
- Type hints encouraged but not required
- Keep functions focused; prefer clarity over cleverness

## Pull requests

- One feature or fix per PR
- Include a brief description of what and why
- If adding a guardrail, follow the G1-G10 pattern in `proactive/guardrails.py`

## Questions?

Open an issue or reach out. This is a research project — thoughtful questions
and suggestions about the organizational theory framing are especially welcome.
