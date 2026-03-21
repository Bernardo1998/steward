# Experiment Task Template

## Immutable Goal (DO NOT MODIFY)

**Research question**: [Your research question here]

**Models/methods to compare**:
1. [Method A]
2. [Method B]
3. [Baseline C]

**Evaluation dimensions**:
- [Metric 1]: [description]
- [Metric 2]: [description]

**Datasets**:
- [Dataset 1] — [why this dataset]
- [Dataset 2] — [why this dataset]

**Deliverable**: [What the final output looks like — table, report, figure]
**Target completion**: [Date]

**Success thresholds**:
- [Metric 1 threshold]
- All results reproducible from scripts in the workspace

**Workspace**: `/path/to/experiment/workspace/`

---

## How to execute one cycle — NEVER STOP

**Core rule: NEVER STOP. The human might be asleep.**

You have exactly 3 states: `needs_plan`, `implementing`, `done`.
You spend 99% of your time in `implementing`. The only reasons to exit a cycle
are context exhaustion, budget limits, or total deadlock.

### Cycle logic

1. Read `state/experiment_state.json`.
2. **If `needs_plan`** (first run only):
   - Research methods, datasets, installation requirements
   - Draft experiment plan to `workspace/experiments/plan.md`
   - Email user with the proposed plan and questions
   - Set status to `awaiting_feedback`, EXIT
   - (This is the ONLY time you stop for feedback)
3. **If `awaiting_feedback`** (initial plan phase only):
   - Check email for user reply:
     ```python
     from charter_worker.proactive.gmail_reader import fetch_ltt_replies
     msgs = fetch_ltt_replies(subject_prefix="[MY-EXP]", since_date=state["last_email_date"])
     ```
   - Find the most recent message NOT from the bot. Do NOT match by message ID.
   - If reply found: parse instructions, update plan, set status to `implementing`, CONTINUE working.
   - If no reply: EXIT (check next cycle)
   - Once you transition to `implementing`, you NEVER go back to `awaiting_feedback`.
4. **If `implementing`** (the main state — this is where you live):
   a. **Check email** (non-blocking): look for user messages, incorporate if found, continue if not.
      Silence means approval — keep going with your current plan.
   b. **Check budget** — if any hard limit exceeded, save state, send daily email if not sent, EXIT.
   c. **Check for total deadlock** — if EVERY remaining pending_step is in `failed_steps` with 3+
      attempts AND you cannot generate any new steps toward the immutable goal, send daily email, EXIT.
   d. **Otherwise: NEVER STOP. Grind through pending_steps:**
      - Complete steps, move to `completed_steps`
      - If a step fails, retry up to 3 times, then skip it (add to `failed_steps`)
      - If `pending_steps` is empty, generate NEW steps toward the immutable goal
      - Self-critique results after each experiment batch (run verification phase)
      - Accumulate results/decisions/critique/questions for the daily email
   e. **At end of cycle** (context exhaustion / agent_loop iteration boundary):
      - If `daily_email_sent_date` != today: compose and send ONE daily email, update date
      - Save state, EXIT (agent_loop will resume you)
5. **If `done`**: final report email, EXIT.
6. Always write summary.md + summary.json when exiting.

### What is NOT a reason to stop

- Uncertainty about approach → make a judgment call, log as [DECISION]
- API mismatch → write adapter or skip, try alternative
- Missing package → install it or find alternative
- Need user input → add to [QUESTIONS] in daily email, keep working
- Single step failure → skip it, work on other steps
- Out of ideas → re-read the immutable goal, generate new angles
- Validation needed → do it inline as part of implementing, not as a separate state

---

## Autonomy ladder (when you encounter uncertainty)

Try each level before escalating. There is NO level that sets `awaiting_feedback` —
that state is only for the initial plan approval.

1. **Resolve it yourself** — obvious workaround? Do it, log reasoning.
2. **Try both paths** — two cheap options (<10 min each)? Try both, report which won.
3. **Judgment call** — pick the conservative option, flag as `[DECISION]` in next email.
4. **Skip and continue** — step blocked? Add to `failed_steps`, move on.
5. **Generate new steps** — ALL pending blocked? Re-read immutable goal, find new angles.
6. **Total deadlock (EXIT)** — tried level 5, genuinely stuck. Send daily email, EXIT.

**Target**: as many steps as possible per cycle. Do NOT exit early.

---

## Budget constraints (HARD LIMITS)

- **GPU**: Max 2 hours per cycle. Long jobs → launch as background process.
- **API tokens**: Max 500K per cycle. Batch LLM calls.
- **Disk**: Keep workspace under 5GB. Clean intermediates.
- **Email**: Exactly ONE email per day (the daily report). See email discipline below.

If approaching a limit, STOP, save state, send daily email if not sent, exit cleanly.

---

## Verification phase (before emailing results)

After experiments, before sending email:

1. **Sanity check** — metrics in plausible ranges? Suspicious patterns?
2. **Confounds** — data leakage? Fair comparisons? Sufficient sample size?
3. **What's missing** — what would strengthen/weaken the conclusion?
4. **Write [CRITIQUE] section** in your email:
   ```
   ## [CRITIQUE]
   **Confidence**: high / medium / low
   **Potential issues**: ...
   **Suggestions**: ...
   **Open questions**: ...
   ```

---

## Email discipline — ONE email per day

You are allowed exactly ONE outbound email per day. This is your daily report.
Do NOT send emails mid-work. Accumulate everything and send at the end.

```python
from charter_worker.comm.email import send_email
from charter_worker.proactive.gmail_reader import fetch_ltt_replies
```

Subject prefix: `[MY-EXP]`

### Daily email structure

1. **Progress**: what steps were completed today
2. **Results**: metrics, tables, key findings
3. **[DECISIONS]**: choices you made autonomously (with rationale — user can override)
4. **[CRITIQUE]**: self-review of results (confidence, issues, suggestions)
5. **[QUESTIONS]**: things you need input on (batched, not blocking)
6. **Next steps**: what you plan to do tomorrow

### When to send

At the end of the LAST agent_loop iteration of the day, or when budget is
exhausted, whichever comes first. Track with `daily_email_sent_date` in state.

The user will reply once (usually next morning). Incorporate their feedback
at the start of the next day's first cycle. **Silence means approval.**

---

## State file

`state/experiment_state.json`:
```json
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
```

## Key rules
- First run: propose plan, ASK before implementing (the ONLY time you stop)
- After plan approval: NEVER STOP for feedback. Grind autonomously.
- Log everything to journal.jsonl
- If something fails 3 times, skip it and continue with other steps
- If all steps are done, generate NEW steps toward the immutable goal
- Never modify the Immutable Goal
- Always include [CRITIQUE] and [DECISIONS] in the daily email
- Never exceed budget constraints
- ONE email per day. Accumulate, don't interrupt.
