"""steward.plan_runner.runner — PlanRunner class.

The :class:`PlanRunner` wraps the v3.1 cycle loop so a per-task ``run.py``
collapses to a 10-line caller::

    from steward.plan_runner import PlanRunner
    PlanRunner(
        task_dir=Path(__file__).parent,
        experiment_repo="/mnt/c/Research/MyProject",
        email_prefix="[MY-PROJECT]",
    ).run()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


class PlanRunner:
    """Reusable plan_runner. Tasks construct one of these in their
    run.py and call ``.run()`` once per cycle.

    Required arguments:
        task_dir: Path to the task folder containing ``goal.md``, ``plan.yaml``.
        experiment_repo: Absolute path where step dispatch runs (Codex
            ``--cd`` target).
        email_prefix: Subject-line prefix, e.g. ``"[MY-RESEARCH]"``. Used
            for inbox filtering + email subject (informational on the
            runner — the live prefix is read from ``goal.md::Orchestrator``
            for the email composer).

    Optional arguments:
        cooldown_hours: int = 3
        budget_cap_usd: float = 50.0
        step_timeout_seconds: Optional[int] = None
            (when None, read from goal.md::experiment_config.step_timeout_seconds,
            falling back to 1800.)
        exploration_log_cap: int = 50
        weaknesses_log_cap: int = 20
        llm_dispatch_when_no_run_command: bool = True
        custom_dispatcher: Optional[Callable] = None
            (override default Codex dispatcher; signature:
             ``(step, task_dir, experiment_repo, intent_answer) -> dict``)
        on_step_complete: Optional[Callable] = None
            (post-completion hook; signature: ``(step, results) -> None``)
        on_cycle_complete: Optional[Callable] = None
            (post-cycle hook; signature:
             ``(state, email_send_result) -> None``)
    """

    def __init__(
        self,
        task_dir: Path,
        experiment_repo: str,
        email_prefix: str,
        cooldown_hours: int = 3,
        budget_cap_usd: float = 50.0,
        step_timeout_seconds: Optional[int] = None,
        exploration_log_cap: int = 50,
        weaknesses_log_cap: int = 20,
        llm_dispatch_when_no_run_command: bool = True,
        custom_dispatcher: Optional[Callable] = None,
        on_step_complete: Optional[Callable] = None,
        on_cycle_complete: Optional[Callable] = None,
    ) -> None:
        self.task_dir = Path(task_dir).resolve()
        self.experiment_repo = Path(experiment_repo)
        self.email_prefix = email_prefix
        self.cooldown_hours = cooldown_hours
        self.budget_cap_usd = budget_cap_usd
        self.step_timeout_seconds = step_timeout_seconds
        self.exploration_log_cap = exploration_log_cap
        self.weaknesses_log_cap = weaknesses_log_cap
        self.llm_dispatch_when_no_run_command = llm_dispatch_when_no_run_command
        self.custom_dispatcher = custom_dispatcher
        self.on_step_complete = on_step_complete
        self.on_cycle_complete = on_cycle_complete

        self.task_id = self.task_dir.name
        self.state_dir = self.task_dir / "state"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute one cycle. Returns the cycle summary dict that was also
        written to ``daily_summaries/<date>/tasks/<id>/summary.json``."""
        # Late imports so the package import is cheap.
        from steward.phases import load_context, send_ltt_email
        from steward.phases.plan_phases import (
            append_deepening_to_plan,
            apply_stage_approval,
            build_email_inputs,
            cap_weaknesses_log,
            check_intent_answer,
            check_stage_approval,
            generate_deepening_actions,
            interpret_reply_nl,
            judge_step_completion,
            load_goal_md,
            load_plan,
            load_state_json,
            reviewer_scrutiny,
            rotate_exploration_log_if_needed,
            save_state_json,
            select_next_action,
            write_current_step_md,
            write_draft_findings_md,
            write_inbox_md,
            write_recap_md,
        )

        self._make_stderr_line_buffered()

        started = time.time()
        date_str = datetime.now().strftime("%Y-%m-%d")
        task_id = self.task_id

        # Make sure steward can find email_config.yaml at the repo root.
        os.environ.setdefault("STEWARD_INSTANCE_ROOT", str(self.task_dir.parent.parent))

        print(f"[{task_id}] plan_runner cycle — {date_str}", file=sys.stderr)

        errors: list[dict] = []

        # --- Load goal.md ------------------------------------------------
        try:
            goal = load_goal_md(self.task_dir)
        except FileNotFoundError as e:
            print(f"[{task_id}] FATAL: goal.md missing: {e}", file=sys.stderr)
            self._write_summary(date_str, {
                "task_id": task_id, "date": date_str, "status": "failed",
                "tldr": [str(e)], "action_items": ["Author goal.md"],
                "errors": [{"type": "goal_md", "message": str(e)}],
                "metadata": {"duration_s": 0, "budget_hint": "low"},
            }, f"# {task_id} — {date_str}\n\nFAILED: {e}\n")
            return {"status": "failed", "error": str(e)}

        definition = self._definition_from_goal(goal)
        exp_cfg = definition["experiment_config"]
        step_timeout = int(
            self.step_timeout_seconds
            or exp_cfg.get("step_timeout_seconds")
            or 1800
        )

        # --- Load plan ---------------------------------------------------
        try:
            plan = load_plan(self.task_dir)
        except (FileNotFoundError, ValueError) as e:
            print(f"[{task_id}] FATAL: plan.yaml problem: {e}", file=sys.stderr)
            self._write_summary(date_str, {
                "task_id": task_id, "date": date_str, "status": "failed",
                "tldr": [f"plan.yaml error: {e}"],
                "action_items": ["Fix plan.yaml"],
                "errors": [{"type": "plan_yaml", "message": str(e)}],
                "metadata": {"duration_s": 0, "budget_hint": "low"},
            }, f"# {task_id} — {date_str}\n\nFAILED: plan.yaml error: {e}\n")
            return {"status": "failed", "error": str(e)}

        # --- Load state.json --------------------------------------------
        state = load_state_json(self.state_dir, plan)

        # --- Cooldown ----------------------------------------------------
        last = state.get("cooldown", {}).get("last_cycle_time")
        if last:
            try:
                delta = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
            except ValueError:
                delta = self.cooldown_hours + 1
            if delta < self.cooldown_hours:
                remaining = self.cooldown_hours - delta
                print(
                    f"[{task_id}] cooldown: {remaining:.1f}h remaining (last={last})",
                    file=sys.stderr,
                )
                summary = {
                    "task_id": task_id, "date": date_str, "status": "skipped",
                    "tldr": [f"Cooldown: {remaining:.1f}h remaining"],
                    "action_items": [], "errors": [],
                    "metadata": {"duration_s": 0, "budget_hint": "low"},
                }
                self._write_summary(
                    date_str, summary,
                    f"# {task_id} — {date_str}\n\nSkipped: cooldown ({remaining:.1f}h remaining)\n",
                )
                return summary

        # --- Load gmail feedback -----------------------------------------
        meta_for_context = {
            "cycle": state.get("cooldown", {}).get("cycle", 0),
            "last_cycle_time": last,
            "days_since_reply": state.get("cooldown", {}).get("days_since_reply", 0),
            "last_message_id": state.get("last_message_id", ""),
            "email_thread_id": state.get("email_thread_id", ""),
            # phase1's reply check is gated on last_email_date; without it the
            # email reply path is never even attempted. subject_prefix scopes
            # the IMAP search to this task's emails.
            "last_email_date": state.get("last_email_date", ""),
            "subject_prefix": state.get("subject_prefix", self.email_prefix),
        }
        feedback: Optional[dict] = None
        try:
            context = load_context(task_id, self.state_dir, meta_for_context)
            feedback = context.get("feedback") or None
        except Exception as e:
            print(f"[{task_id}] load_context failed (continuing): {e}", file=sys.stderr)
            errors.append({"phase": "context", "error": str(e)})

        # --- Dump inbox before approval check ---------------------------
        write_inbox_md(self.state_dir, feedback)

        # --- Reply de-dup -----------------------------------------------
        reply_msgid = (feedback or {}).get("reply_msgid")

        def _reply_fresh() -> bool:
            # Fresh = carries an id we haven't already consumed for an approval.
            # No id → treat as fresh (can't dedup); file-edit path is never gated.
            return (not reply_msgid) or reply_msgid != state.get("last_reply_approval_msgid")

        # --- Stage approval check (explicit /approve marker or file edit) ---
        approved_applied = False
        appr = check_stage_approval(self.state_dir, state, feedback=feedback)
        if appr.get("approved"):
            is_reply = appr.get("method") in ("email_reply", "nl_reply")
            if (not is_reply) or _reply_fresh():
                print(
                    f"[{task_id}] Stage approval detected ({appr.get('method')}) "
                    f"for stage '{appr.get('stage_id')}'",
                    file=sys.stderr,
                )
                apply_stage_approval(self.state_dir, state, appr)
                approved_applied = True
                if is_reply:
                    state["last_reply_approval_msgid"] = reply_msgid
            else:
                print(f"[{task_id}] approval marker found but this reply was already "
                      f"used to approve an earlier gate; ignoring", file=sys.stderr)

        # --- NL (plain-English) approval fallback -----------------------
        # Only when the marker path didn't approve and the reply is fresh.
        # interpret_reply_nl no-ops unless a gate is currently armed.
        if not approved_applied and feedback and _reply_fresh():
            try:
                nli = interpret_reply_nl(feedback, state, plan)
            except Exception as e:
                print(f"[{task_id}] reply interpreter failed: {e}", file=sys.stderr)
                nli = {}
            if nli.get("approve"):
                appr_nl = {
                    "approved": True,
                    "method": "nl_reply",
                    "stage_id": nli.get("stage_id"),
                    "marker": (nli.get("interpretation") or "")[:120],
                }
                print(
                    f"[{task_id}] Stage approval detected (nl_reply) "
                    f"for stage '{appr_nl.get('stage_id')}'",
                    file=sys.stderr,
                )
                apply_stage_approval(self.state_dir, state, appr_nl)
                approved_applied = True
                state["last_reply_approval_msgid"] = reply_msgid

        # --- Intent answer check ----------------------------------------
        intent_answer = check_intent_answer(self.state_dir, plan, feedback, state)
        pending = state.get("pending_intent_question")
        if intent_answer and pending:
            print(
                f"[{task_id}] Intent answer for step {pending.get('step_id')!r}: "
                f"{intent_answer[:120]!r}",
                file=sys.stderr,
            )
            state["pending_intent_question"] = None

        # --- Select next action -----------------------------------------
        cycle = state.get("cooldown", {}).get("cycle", 0) + 1
        cycle_action = select_next_action(
            plan, state, deepening_queue=plan.get("deepening_added_by_runner")
        )
        print(
            f"[{task_id}] Cycle {cycle} selected action: {cycle_action.get('kind')}",
            file=sys.stderr,
        )

        # --- Write recap.md + current_step.md BEFORE dispatch -----------
        write_recap_md(self.state_dir, plan, state, goal)
        write_current_step_md(self.state_dir, plan, state, cycle_action)

        completed_bundles: list[dict] = []
        new_findings: list[dict] = []

        # --- Dispatch ----------------------------------------------------
        if cycle_action.get("kind") in ("plan_step", "deepening"):
            step = cycle_action.get("step") or cycle_action.get("action") or {}
            step_id = step["id"]
            progress = state["step_progress"].setdefault(step_id, {
                "status": "pending", "attempts": 0, "completed_at": None,
                "last_results_path": None, "deepening_cycles_done": 0,
                "deepening_actions_generated": [],
            })
            progress["status"] = "in_progress"
            progress["attempts"] = (progress.get("attempts") or 0) + 1

            dispatch_result = self._dispatch_step(
                cycle_action, task_id, cycle, step_timeout,
                definition=definition,
                intent_answer=intent_answer,
            )

            if dispatch_result.get("intent_question"):
                now_iso = datetime.now().isoformat(timespec="seconds")
                state["pending_intent_question"] = {
                    "step_id": step_id,
                    "question": dispatch_result["intent_question"],
                    "asked_at": now_iso,
                }
                progress["attempts"] = max(0, (progress.get("attempts") or 1) - 1)
                progress["status"] = "pending"
                print(
                    f"[{task_id}] INTENT_NEEDED on {step_id!r}: "
                    f"{dispatch_result['intent_question'][:120]!r}",
                    file=sys.stderr,
                )
                judgment = {"status": "in_progress", "rationale": "intent_question pending"}
                bundle = {
                    "step_id": step_id,
                    "description": step.get("description", ""),
                    "completed_at": now_iso,
                    "results": dispatch_result.get("results"),
                    "attempts": progress["attempts"],
                }
            else:
                bundle = {
                    "step_id": step_id,
                    "description": step.get("description", ""),
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                    "results": dispatch_result.get("results"),
                    "attempts": progress["attempts"],
                }
                judgment = judge_step_completion(
                    step, bundle, definition, self.experiment_repo,
                )

            progress["last_results_path"] = (
                dispatch_result["log_entry"].get("results_path")
                or progress.get("last_results_path")
            )

            if judgment["status"] == "completed":
                progress["status"] = "completed"
                progress["completed_at"] = bundle["completed_at"]
                completed_bundles.append(bundle)
                new_findings.append({
                    "step_id": step_id,
                    "finding": (judgment.get("rationale") or step.get("description", ""))[:300],
                    "source": progress.get("last_results_path") or "(no results file)",
                })
                if self.on_step_complete:
                    try:
                        self.on_step_complete(step, dispatch_result.get("results"))
                    except Exception as e:
                        print(f"[{task_id}] on_step_complete hook failed: {e}",
                              file=sys.stderr)
                        errors.append({"phase": "on_step_complete", "error": str(e)})

                if cycle_action.get("kind") == "plan_step":
                    try:
                        new_deepening = generate_deepening_actions(step, bundle, definition, n=3)
                    except Exception as e:
                        print(f"[{task_id}] generate_deepening_actions failed: {e}",
                              file=sys.stderr)
                        new_deepening = []
                    if new_deepening:
                        append_deepening_to_plan(self.task_dir, plan, new_deepening)
                        progress["deepening_actions_generated"] = list(set(
                            progress.get("deepening_actions_generated", [])
                            + [a["id"] for a in new_deepening]
                        ))
                elif cycle_action.get("kind") == "deepening":
                    parent_id = step.get("parent_step_id")
                    if parent_id and parent_id in state["step_progress"]:
                        parent_prog = state["step_progress"][parent_id]
                        parent_prog["deepening_cycles_done"] = (
                            parent_prog.get("deepening_cycles_done", 0) + 1
                        )

            elif judgment["status"] == "needs_deepening":
                progress["status"] = "deepening"
                completed_bundles.append(bundle)
                # A step that passed hard criteria but failed the soft rubric
                # must still generate deepening work, else it parks in
                # `deepening` with an empty queue and wedges the plan forever.
                if cycle_action.get("kind") == "plan_step":
                    try:
                        new_deepening = generate_deepening_actions(step, bundle, definition, n=3)
                    except Exception as e:
                        print(f"[{task_id}] generate_deepening_actions failed: {e}",
                              file=sys.stderr)
                        new_deepening = []
                    if new_deepening:
                        append_deepening_to_plan(self.task_dir, plan, new_deepening)
                        progress["deepening_actions_generated"] = list(set(
                            progress.get("deepening_actions_generated", [])
                            + [a["id"] for a in new_deepening]
                        ))
                elif cycle_action.get("kind") == "deepening":
                    # Advance the budget even when the deepening action itself
                    # fails its soft rubric, so it can't re-run indefinitely.
                    parent_id = step.get("parent_step_id")
                    if parent_id and parent_id in state["step_progress"]:
                        parent_prog = state["step_progress"][parent_id]
                        parent_prog["deepening_cycles_done"] = (
                            parent_prog.get("deepening_cycles_done", 0) + 1
                        )
            elif judgment["status"] == "failed":
                progress["status"] = "failed"
                errors.append({
                    "phase": "step_judgment",
                    "error": f"{step_id}: {judgment.get('rationale', '')}",
                })
            else:
                progress["status"] = "pending"

            if judgment["status"] in ("completed", "needs_deepening"):
                state["stuck_counter"] = 0
            else:
                state["stuck_counter"] = state.get("stuck_counter", 0) + 1

        # --- Promote spent `deepening` steps to `done_enough` -----------
        # A step stays in `deepening` only while it still has an eligible
        # queued deepening action under its budget. Once the budget is spent
        # (or no deepening action was ever generated), promote it to
        # `done_enough` so `_advance_stages` can fire the stage-approval gate
        # instead of the plan wedging on `stuck` forever.
        deep_queue = plan.get("deepening_added_by_runner") or []
        step_caps = {
            s["id"]: int(s.get("max_deepening_cycles", 4) or 4)
            for s in plan.get("steps", []) or []
        }
        promote_iso = datetime.now().isoformat(timespec="seconds")
        for sid, prog in state["step_progress"].items():
            if prog.get("status") != "deepening":
                continue
            cap = step_caps.get(sid, 4)
            done = prog.get("deepening_cycles_done", 0)
            has_eligible = any(
                act.get("parent_step_id") == sid
                and state["step_progress"].get(act["id"], {}).get("status", "pending")
                in ("pending", "deepening")
                for act in deep_queue
            )
            if done >= cap or not has_eligible:
                prog["status"] = "done_enough"
                prog["completed_at"] = prog.get("completed_at") or promote_iso

        # --- Stage advancement bookkeeping ------------------------------
        self._advance_stages(plan, state)

        # --- Same-cycle gate approval from an already-waiting reply -----
        # Gates arm at the END of a cycle (just above), but replies are read
        # at the START, so a reply already waiting when the gate arms would
        # otherwise not be honored until the next cycle (a cooldown later).
        # Re-check the reply against the just-armed gate so it takes effect now.
        if (feedback and not approved_applied
                and state.get("awaiting_stage_approval") and _reply_fresh()):
            appr2 = check_stage_approval(self.state_dir, state, feedback=feedback)
            if not (appr2.get("approved")
                    and appr2.get("method") in ("email_reply", "nl_reply")):
                try:
                    nli2 = interpret_reply_nl(feedback, state, plan)
                except Exception as e:
                    print(f"[{task_id}] post-advance reply interpreter failed: {e}",
                          file=sys.stderr)
                    nli2 = {}
                if nli2.get("approve"):
                    appr2 = {"approved": True, "method": "nl_reply",
                             "stage_id": nli2.get("stage_id"),
                             "marker": (nli2.get("interpretation") or "")[:120]}
            if appr2.get("approved"):
                apply_stage_approval(self.state_dir, state, appr2)
                approved_applied = True
                state["last_reply_approval_msgid"] = reply_msgid
                print(f"[{task_id}] gate '{appr2.get('stage_id')}' approved "
                      f"same-cycle from a waiting reply ({appr2.get('method')})",
                      file=sys.stderr)

        # --- Reviewer scrutiny ------------------------------------------
        progress_map = state.get("step_progress", {})
        all_completed_bundles: list[dict] = []
        for step in plan.get("steps", []) or []:
            sid = step["id"]
            prog = progress_map.get(sid, {})
            if prog.get("status") not in ("completed", "done_enough", "deepening"):
                continue
            results_path = self.experiment_repo / "results" / f"{sid}_results.json"
            results_data = None
            if results_path.exists():
                try:
                    results_data = json.loads(results_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            all_completed_bundles.append({
                "step_id": sid,
                "description": step.get("description", ""),
                "completed_at": prog.get("completed_at"),
                "results": results_data,
            })

        try:
            scrutiny = reviewer_scrutiny(plan, state, all_completed_bundles[-6:], definition)
        except Exception as e:
            print(f"[{task_id}] reviewer_scrutiny failed: {e}", file=sys.stderr)
            errors.append({"phase": "reviewer_scrutiny", "error": str(e)})
            scrutiny = {"weaknesses": [], "missing_ablations": [],
                        "would_a_reviewer_accept_this_now": False, "rationale": ""}

        # --- Draft findings (latest-only) -------------------------------
        try:
            findings_body = self._compose_findings_body(
                plan, state, cycle, new_findings, completed_bundles, scrutiny, cycle_action,
            )
            write_draft_findings_md(self.state_dir, findings_body)
        except Exception as e:
            print(f"[{task_id}] write_draft_findings_md failed: {e}", file=sys.stderr)
            errors.append({"phase": "draft_findings", "error": str(e)})

        # --- Build email inputs + send ----------------------------------
        email_inputs = build_email_inputs(
            self.task_dir, plan, state, cycle_action,
            completed_bundles=all_completed_bundles[-6:],
            diff=None,
        )

        email_result: dict = {"status": "skipped"}
        try:
            projects_status = [{
                "project_id": task_id,
                "status": {
                    "cycle_number": cycle,
                    "current_hypothesis": goal.get("goal", ""),
                    "confidence_score": "n/a",
                    "needs_human_input": bool(
                        email_inputs.get("awaiting_stage_approval")
                        or email_inputs.get("awaiting_approval")
                    ),
                    "open_questions": [],
                    "action_suggestions": [],
                    "key_findings": [],
                },
                "definition": definition,
                "task_dir": str(self.task_dir),
                "cycle_summary": {"tldr": email_inputs.get("plan_title", "")[:100]},
                "days_since_reply": state.get("cooldown", {}).get("days_since_reply", 0),
                "plan_runner_summary": email_inputs,
            }]
            email_result, _ = send_ltt_email(projects_status, meta_for_context, dry_run=False)
            print(f"[{task_id}] email: {email_result.get('status')}", file=sys.stderr)
            if email_result.get("message_id"):
                state["last_message_id"] = email_result["message_id"]
            if meta_for_context.get("email_thread_id"):
                state["email_thread_id"] = meta_for_context["email_thread_id"]
            if email_result.get("status") == "sent":
                # Arm the reply check for next cycle: phase1 only fetches
                # replies once last_email_date is set, scoped by subject_prefix.
                state["last_email_date"] = date_str
                state["subject_prefix"] = self.email_prefix
        except Exception as e:
            print(f"[{task_id}] email send failed: {e}", file=sys.stderr)
            errors.append({"phase": "email", "error": str(e)})

        # --- End-of-cycle bookkeeping -----------------------------------
        cap_weaknesses_log(state, self.state_dir, cap=self.weaknesses_log_cap)
        rotate_exploration_log_if_needed(self.state_dir, cap=self.exploration_log_cap)

        state["plan_version"] = plan.get("version", state.get("plan_version", 1))
        state["cooldown"]["last_cycle_time"] = datetime.now().isoformat()
        state["cooldown"]["cycle"] = cycle
        save_state_json(self.state_dir, state)

        # --- Summary -----------------------------------------------------
        duration = time.time() - started
        progress_pairs = [
            (s["id"], progress_map.get(s["id"], {}).get("status", "pending"))
            for s in plan.get("steps", []) or []
        ]
        steps_done = sum(
            1 for _, st in progress_pairs if st in ("completed", "done_enough")
        )
        steps_total = len(progress_pairs)

        tldr_parts = [
            f"Cycle {cycle}",
            f"action={cycle_action.get('kind')}",
            f"steps_completed={steps_done}/{steps_total}",
        ]
        awaiting = state.get("awaiting_stage_approval")
        if awaiting:
            tldr_parts.append(
                f"awaiting_stage_approval (stage '{awaiting.get('completed_stage_id')}')"
            )

        summary_json = {
            "task_id": task_id,
            "date": date_str,
            "status": "success" if not errors else "partial",
            "tldr": [" · ".join(tldr_parts)],
            "action_items": (
                [
                    f"Approve stage '{awaiting['completed_stage_id']}' "
                    f"(/approve {awaiting['completed_stage_id']} or /approve next)"
                ]
                if awaiting else []
            ),
            "errors": [{"type": e["phase"], "message": e["error"]} for e in errors],
            "metadata": {
                "duration_s": round(duration, 1),
                "budget_hint": "medium",
                "plan_version": plan.get("version"),
                "awaiting_stage_approval": bool(awaiting),
                "steps_completed": steps_done,
                "steps_total": steps_total,
                "stuck_counter": state.get("stuck_counter", 0),
                "email_status": email_result.get("status"),
                "stage_progress_banner": email_inputs.get("stage_progress_banner"),
            },
        }
        md_lines = [
            f"# {task_id} — {date_str}",
            "",
            "## TL;DR",
            f"- Cycle {cycle} · action={cycle_action.get('kind')} · "
            f"steps_completed={steps_done}/{steps_total}",
        ]
        if awaiting:
            md_lines.append(
                f"- Stage `{awaiting['completed_stage_id']}` awaiting approval. "
                f"Reply `/approve {awaiting['completed_stage_id']}` or `/approve next`."
            )
        md_lines.append("")
        md_lines.append("## Per-step status")
        for sid, st in progress_pairs:
            md_lines.append(f"- {sid}: {st}")
        if errors:
            md_lines.append("")
            md_lines.append("## Errors")
            for e in errors:
                md_lines.append(f"- {e['phase']}: {e['error']}")
        md_lines.append("")
        md_lines.append(f"*Duration: {duration:.1f}s · email: {email_result.get('status')}*")

        self._write_summary(date_str, summary_json, "\n".join(md_lines))
        print(f"[{task_id}] complete in {duration:.1f}s", file=sys.stderr)

        if self.on_cycle_complete:
            try:
                self.on_cycle_complete(state, email_result)
            except Exception as e:
                print(f"[{task_id}] on_cycle_complete hook failed: {e}",
                      file=sys.stderr)

        return summary_json

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_stderr_line_buffered(self) -> None:
        """Best-effort line buffering for direct CLI runs.

        Some task wrappers capture stderr with in-memory streams. Those
        streams intentionally do not expose ``fileno()``, so this must not be
        required for the runner to execute.
        """
        try:
            sys.stderr.reconfigure(line_buffering=True)
            return
        except (AttributeError, OSError, ValueError):
            pass

        try:
            fileno = sys.stderr.fileno()
        except (AttributeError, OSError, ValueError):
            return

        try:
            sys.stderr = os.fdopen(fileno, "w", buffering=1)
        except (OSError, ValueError):
            return

    def _definition_from_goal(self, goal: dict) -> dict:
        """Build a v2-shaped definition dict for downstream LLM helpers
        (judge_soft_criteria, generate_deepening_actions, reviewer_scrutiny)
        that still consume a definition shape."""
        return {
            "project_id": self.task_id,
            "name": self.task_id,
            "goal": goal.get("goal", ""),
            "things_to_show": goal.get("things_to_show") or [],
            "scope_boundaries": {
                "in_scope": goal.get("scope_in") or [],
                "out_of_scope": goal.get("scope_out") or [],
            },
            "experiment_repo": str(self.experiment_repo),
            "experiment_config": goal.get("experiment_config") or {},
        }

    def _advance_stages(self, plan: dict, state: dict) -> None:
        """Mark the first un-approved stage `completed` if every step in it
        is completed/done_enough, and arm awaiting_stage_approval."""
        stages = plan.get("stages") or [
            {"id": "all", "steps": [s["id"] for s in plan.get("steps") or []]}
        ]
        step_prog = state["step_progress"]
        now_iso = datetime.now().isoformat(timespec="seconds")
        for st in stages:
            sid = st["id"]
            sp = state["stage_progress"].setdefault(sid, {
                "status": "pending", "started_at": None, "completed_at": None,
                "approved_at": None, "steps_done": 0,
                "steps_total": len(st.get("steps") or []),
            })
            if sp.get("status") == "approved":
                continue
            done_count = sum(
                1 for s_id in st["steps"]
                if step_prog.get(s_id, {}).get("status") in ("completed", "done_enough")
            )
            any_in_progress = any(
                step_prog.get(s_id, {}).get("status") == "in_progress"
                for s_id in st["steps"]
            )
            sp["steps_done"] = done_count
            if done_count >= len(st["steps"]) and not any_in_progress:
                sp["status"] = "completed"
                sp["completed_at"] = sp.get("completed_at") or now_iso
                if not state.get("awaiting_stage_approval"):
                    state["awaiting_stage_approval"] = {
                        "completed_stage_id": sid,
                        "asked_at": now_iso,
                    }
                break
            elif done_count > 0:
                sp["status"] = "in_progress"
                if not sp.get("started_at"):
                    sp["started_at"] = now_iso

    def _compose_findings_body(
        self,
        plan: dict,
        state: dict,
        cycle: int,
        new_findings: list[dict],
        completed_bundles: list[dict],
        scrutiny: dict,
        cycle_action: dict,
    ) -> str:
        """Compose the body for state/draft_findings.md. Single section, no
        accumulation."""
        lines: list[str] = []
        lines.append(f"## Cycle {cycle} summary")
        lines.append("")
        kind = cycle_action.get("kind", "?")
        target = (
            (cycle_action.get("step") or cycle_action.get("action") or {}).get("id")
            or cycle_action.get("stage_id") or "(n/a)"
        )
        lines.append(f"- **Action:** {kind} ({target})")
        if new_findings:
            lines.append("- **New findings:**")
            for nf in new_findings[:6]:
                sid = nf.get("step_id", "?")
                text = (nf.get("finding") or "").strip()[:300]
                lines.append(f"  - **{sid}**: {text}")
        if completed_bundles:
            lines.append(
                "- **Bundles touched this cycle:** "
                + ", ".join(b.get("step_id", "?") for b in completed_bundles)
            )
        weaknesses = scrutiny.get("weaknesses") or []
        if weaknesses:
            lines.append("- **Reviewer concerns surfaced:**")
            for w in weaknesses[:3]:
                wt = (w.get("weakness") or "").strip()
                sid = w.get("step_id", "?")
                if wt:
                    lines.append(f"  - (re: {sid}) {wt}")
        awaiting = state.get("awaiting_stage_approval")
        if awaiting:
            lines.append(
                f"- **Approval gate:** stage `{awaiting.get('completed_stage_id')}` "
                "completed — awaiting `/approve <stage_id>` or `/approve next`."
            )
        return "\n".join(lines)

    def _append_exploration_log(self, entry: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "exploration_log.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _write_summary(self, date_str: str, payload_json: dict, payload_md: str) -> None:
        repo_root = self.task_dir.parent.parent
        summary_dir = repo_root / "daily_summaries" / date_str / "tasks" / self.task_id
        summary_dir.mkdir(parents=True, exist_ok=True)
        with open(summary_dir / "summary.json", "w") as f:
            json.dump(payload_json, f, indent=2, ensure_ascii=False)
        with open(summary_dir / "summary.md", "w") as f:
            f.write(payload_md)

    def _dispatch_step(
        self,
        action: dict,
        task_id: str,
        cycle: int,
        step_timeout_seconds: int,
        *,
        definition: dict,
        intent_answer: Optional[str] = None,
    ) -> dict:
        """Execute one plan step or deepening action."""
        from steward.phases.plan_phases import default_codex_dispatcher

        step = action.get("step") or action.get("action") or {}
        step_id = step.get("id", "?")
        run_command = step.get("run_command")
        started_at = datetime.now().isoformat(timespec="seconds")
        log_entry: dict[str, Any] = {
            "cycle": cycle,
            "timestamp": started_at,
            "type": "experiment" if action.get("kind") in ("plan_step", "deepening") else "research",
            "step_id": step_id,
            "kind": action.get("kind"),
            "status": "ran",
        }
        intent_question: Optional[str] = None
        cost_usd: Optional[float] = None
        commits: list[dict] = []

        exp_cfg = definition.get("experiment_config") or {}

        if self.custom_dispatcher is not None:
            print(
                f"[{task_id}] Dispatching step {step_id!r} via custom_dispatcher",
                file=sys.stderr,
            )
            self.experiment_repo.mkdir(parents=True, exist_ok=True)
            cdx = self.custom_dispatcher(
                step, self.task_dir, self.experiment_repo, intent_answer,
            ) or {}
            stdout = cdx.get("stdout", "")
            stderr = cdx.get("stderr", "")
            returncode = cdx.get("returncode")
            intent_question = cdx.get("intent_question")
            cost_usd = cdx.get("cost_usd")
            commits = cdx.get("commits") or []
            log_entry["status"] = cdx.get("status", "ran")
        elif run_command:
            self.experiment_repo.mkdir(parents=True, exist_ok=True)
            print(
                f"[{task_id}] Dispatching step {step_id!r} via run_command "
                f"(timeout {step_timeout_seconds}s)",
                file=sys.stderr,
            )
            try:
                proc = subprocess.run(
                    run_command, shell=True, cwd=str(self.experiment_repo),
                    capture_output=True, text=True, timeout=step_timeout_seconds,
                )
                stdout = proc.stdout
                stderr = proc.stderr
                returncode = proc.returncode
                log_entry["status"] = "success" if returncode == 0 else "failed"
                log_entry["returncode"] = returncode
                log_entry["conclusion"] = (
                    (stdout[-400:] if stdout else "") + (stderr[-400:] if stderr else "")
                )[:800]
            except subprocess.TimeoutExpired as e:
                stdout, stderr, returncode = (e.stdout or ""), (e.stderr or ""), None
                log_entry["status"] = "timeout"
                log_entry["conclusion"] = f"timeout after {step_timeout_seconds}s"
        elif self.llm_dispatch_when_no_run_command:
            print(
                f"[{task_id}] Dispatching step {step_id!r} via Codex sub-agent "
                f"(timeout {step_timeout_seconds}s; intent_answer="
                f"{'yes' if intent_answer else 'no'})",
                file=sys.stderr,
            )
            self.experiment_repo.mkdir(parents=True, exist_ok=True)
            cdx = default_codex_dispatcher(
                step, definition, self.experiment_repo, self.task_dir, cycle,
                step_timeout_seconds,
                intent_answer=intent_answer,
                model=exp_cfg.get("model"),
            )
            stdout = cdx["stdout"]
            stderr = cdx["stderr"]
            returncode = cdx["returncode"]
            intent_question = cdx.get("intent_question")
            cost_usd = cdx.get("cost_usd")
            commits = cdx.get("commits") or []
            log_entry["status"] = cdx["status"]
            log_entry["returncode"] = returncode
            log_entry["conclusion"] = (
                (stdout[-400:] if stdout else "") + (stderr[-400:] if stderr else "")
            )[:800]
            if intent_question:
                log_entry["intent_question"] = intent_question
            if cost_usd is not None:
                log_entry["cost_usd"] = cost_usd
            if commits:
                log_entry["commits"] = commits
        else:
            stdout = stderr = ""
            returncode = None
            log_entry["status"] = "skipped"
            log_entry["conclusion"] = (
                "step has no run_command and llm_dispatch_when_no_run_command "
                "is false"
            )

        # Try to load conventional results JSON.
        results_path = self.experiment_repo / "results" / f"{step_id}_results.json"
        results_data: Optional[dict] = None
        if results_path.exists():
            try:
                results_data = json.loads(results_path.read_text(encoding="utf-8"))
                log_entry["results_path"] = str(results_path)
            except json.JSONDecodeError as e:
                log_entry["results_parse_error"] = str(e)

        self._append_exploration_log(log_entry)

        return {
            "status": log_entry["status"],
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
            "results": results_data,
            "intent_question": intent_question,
            "cost_usd": cost_usd,
            "commits": commits,
            "log_entry": log_entry,
            "step": step,
        }
