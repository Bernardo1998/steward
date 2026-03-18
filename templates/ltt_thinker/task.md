# Long-Term Thinker — Task Instructions

## What you are

A proactive research agent that tracks a hypothesis over many cycles. Each cycle
you: check for human feedback, research open questions, synthesize findings,
send an email report, and explore speculative directions.

## How to execute one cycle

Run the proactive cycle script:
```bash
python run.py
```

This runs the full 5-phase cycle:
1. **Context** — Load state, check email replies, detect stagnation
2. **Research** — Generate queries from open questions, search via web
3. **Synthesize** — Extract claims, check provenance, update hypothesis
4. **Email** — Self-review, compose report, send to user
5. **Speculate** — Lightweight exploration of promising directions

## Key files
- `definition.yaml` — Project goal, scope, success criteria (DO NOT MODIFY the goal)
- `state/status.yaml` — Current hypothesis, findings, open questions, confidence
- `state/exploration_log.jsonl` — All queries and results (append-only)
- `state/task_state.json` — Cycle count, email threading, metrics history
- `context_files/` — Drop reference documents here (PDFs, papers, notes)

## Email feedback loop
- The agent sends reports with the subject prefix from charter.yaml
- Reply to the email to give feedback, corrections, or new priorities
- Or send a fresh email to the bot address with the same prefix
- The agent reads replies via IMAP on the next cycle

## Guardrails (automatic)
- G1: Relevance gate — scores claims against project goal
- G2: Dedup — skips queries similar to recent ones
- G3: Provenance — labels unsourced claims as [HYPOTHESIS]
- G4: Novelty — flags when suggestions are stale
- G5: Size cap — evicts low-scoring items when lists overflow
- G6: Self-review — quality check before sending email
- G7: Stagnation — flags when 2+ cycles show no progress
- G9: Speculative isolation — keeps speculative work separate from main status
- G10: Feedback integration — parses human replies into structured actions
