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

## How to execute one cycle

1. Read `state/experiment_state.json` to understand where you are.
2. If status is `"needs_plan"` (first cycle):
   - Research the methods and datasets
   - Draft an experiment plan to `workspace/experiments/plan.md`
   - Email the user with the plan and questions
   - Set status to `"awaiting_feedback"` — do NOT implement yet
3. If status is `"awaiting_feedback"`:
   - Check email for user messages:
     ```python
     from charter_worker.proactive.gmail_reader import fetch_ltt_replies
     msgs = fetch_ltt_replies(subject_prefix="[MY-EXP]", since_date=state["last_email_date"])
     ```
   - Find the most recent message NOT from the bot. Do NOT match by message ID.
   - If found: parse instructions, update plan, set status to `"implementing"`
   - If not found: exit (will check again next cycle)
   - IMPORTANT: If transitioning to implementing, CONTINUE in the same cycle.
4. If status is `"implementing"`:
   - Read `pending_steps` and `completed_steps` from state
   - Work through pending steps sequentially. Do as many as you can.
   - Follow the **autonomy ladder** and **budget constraints** below.
   - Save state before exiting.
5. If status is `"validating"`:
   - Check outputs against success criteria
   - Run the **verification phase** below
   - Email results + critique section
6. Always write summary.md + summary.json when done.

---

## Autonomy ladder (when you encounter uncertainty)

Try each level before escalating:

1. **Resolve it yourself** — obvious workaround? Do it, log reasoning.
2. **Try both paths** — two cheap options (<10 min each)? Try both, report which won.
3. **Judgment call** — pick the conservative option, flag as `[DECISION]` in next email.
4. **Skip and continue** — step blocked? Add to `failed_steps`, move on.
5. **Block (last resort)** — ALL pending steps blocked? Set `awaiting_feedback`.

**Target**: 3-5 steps per cycle. Don't exit early.

---

## Budget constraints (HARD LIMITS)

- **GPU**: Max 2 hours per cycle. Long jobs → launch as background process.
- **API tokens**: Max 500K per cycle. Batch LLM calls.
- **Disk**: Keep workspace under 5GB. Clean intermediates.
- **Email**: Max 3 per cycle. Combine results.

If approaching a limit, STOP, save state, exit cleanly.

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

## Email

```python
from charter_worker.comm.email import send_email
from charter_worker.proactive.gmail_reader import fetch_ltt_replies
```

Subject prefix: `[MY-EXP]`

## State file

`state/experiment_state.json`:
```json
{
  "status": "needs_plan",
  "current_step": 0,
  "last_email_date": "",
  "notes": "",
  "completed_steps": [],
  "pending_steps": [],
  "failed_steps": [],
  "budget_used": {"gpu_minutes": 0, "api_tokens": 0, "emails_sent": 0}
}
```

## Key rules
- First run: propose plan, ASK before implementing
- Log everything to journal.jsonl
- Never modify the Immutable Goal
- Always include [CRITIQUE] in result emails
- Never exceed budget constraints
