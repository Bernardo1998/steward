"""Generic CycleRunner — the reusable skeleton for iterative tasks.

For tasks that follow a plan → act → reflect cycle (research, experiments).
Pipeline tasks (paper_reader, job_search) should use custom run.py instead.

Usage (minimal — just definition.yaml):
    python -m steward.runner --definition definition.yaml --state state/

Usage (custom task with overrides):
    class MyTask(CycleRunner):
        def plan(self, context):
            return [Action("web_search", query="...")]

    MyTask(definition="definition.yaml", state_dir="state/").run_cycle()
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .actions import Action, ActionResult, BUILT_IN_ACTIONS


class CycleRunner:
    """Generic runner for iterative thinking/experiment tasks.

    Six phases: load_state → plan → execute → summarize → report → save_state.
    Override individual phases for custom behavior.
    """

    # Override this dict to register custom action types
    custom_actions: dict = {}

    def __init__(
        self,
        definition: Path | str,
        state_dir: Path | str,
        summary_dir: Optional[Path | str] = None,
        task_id: Optional[str] = None,
    ):
        self.definition_path = Path(definition)
        if not self.definition_path.exists():
            raise FileNotFoundError(f"Definition not found: {self.definition_path}")

        with open(self.definition_path) as f:
            self.definition = yaml.safe_load(f) or {}

        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.task_id = task_id or self.definition.get(
            "project_id",
            self.definition_path.parent.name,
        )

        # Summary output directory
        if summary_dir:
            self.summary_dir = Path(summary_dir)
        else:
            # Try env var from orchestrator, fallback to local
            env_dir = os.environ.get("STEWARD_SUMMARY_DIR")
            if env_dir:
                self.summary_dir = Path(env_dir)
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")
                instance_root = os.environ.get("STEWARD_INSTANCE_ROOT", ".")
                self.summary_dir = (
                    Path(instance_root) / "daily_summaries" / date_str
                    / "tasks" / self.task_id
                )

        # Instance-level copy of custom_actions (class var is shared)
        if not hasattr(self, '_instance_custom_actions'):
            self.custom_actions = dict(self.__class__.custom_actions)

        # Merge built-in + custom actions
        self._actions = {**BUILT_IN_ACTIONS, **self.custom_actions}

    # ------------------------------------------------------------------
    # Phase 1: Load State
    # ------------------------------------------------------------------

    def load_state(self) -> dict:
        """Read all state files + definition into a context dict.

        Returns:
            context dict with keys:
                definition: dict (from definition.yaml)
                state_files: {filename: parsed_content}
                status: dict (from status.yaml if it exists, else empty)
                meta: dict (from task_state.json if it exists, else defaults)
        """
        state_files = {}
        for path in sorted(self.state_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            try:
                if name.endswith(".yaml") or name.endswith(".yml"):
                    with open(path) as f:
                        state_files[name] = yaml.safe_load(f) or {}
                elif name.endswith(".json"):
                    with open(path) as f:
                        state_files[name] = json.load(f)
                elif name.endswith(".jsonl"):
                    entries = []
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    entries.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    state_files[name] = entries
                elif name.endswith((".md", ".txt", ".csv")):
                    state_files[name] = path.read_text(encoding="utf-8", errors="replace")
                # Skip binary files
            except Exception as e:
                print(f"  [runner] Warning: failed to load {name}: {e}", file=sys.stderr)

        # Extract well-known state structures
        status = state_files.get("status.yaml", {})
        meta = state_files.get("task_state.json", {
            "cycle": 0,
            "days_since_reply": 0,
            "status": "active",
        })

        return {
            "definition": self.definition,
            "state_files": state_files,
            "status": status,
            "meta": meta,
            "task_id": self.task_id,
        }

    # ------------------------------------------------------------------
    # Phase 2: Plan
    # ------------------------------------------------------------------

    def plan(self, context: dict) -> list[Action]:
        """Decide which actions to run this cycle.

        Default: generate web_search or lightweight_search from open questions.
        Override for custom planning logic (budget rotation, experiment gating, etc.)
        """
        actions_config = self.definition.get("actions", {})
        status = context.get("status", {})
        open_questions = status.get("open_questions", [])

        actions = []

        # Web search if enabled
        ws_config = actions_config.get("web_search", {})
        if ws_config.get("enabled", False):
            # Use first open question as query, or fall back to goal
            if open_questions:
                q = open_questions[0]
                query = q.get("question", q) if isinstance(q, dict) else str(q)
            else:
                query = self.definition.get("goal", "")

            if query:
                actions.append(Action(
                    "lightweight_search",
                    query=query,
                    config=ws_config,
                ))

        # Add any other enabled action types
        for action_type, config in actions_config.items():
            if action_type == "web_search":
                continue  # already handled
            if isinstance(config, dict) and config.get("enabled", False):
                actions.append(Action(action_type, config=config))

        return actions

    # ------------------------------------------------------------------
    # Phase 3: Execute
    # ------------------------------------------------------------------

    def execute(self, actions: list[Action], context: dict) -> list[ActionResult]:
        """Dispatch each action and collect results.

        Failures are caught per-action — one bad action doesn't stop the rest.
        """
        results = []
        for action in actions:
            try:
                result = self._dispatch_action(action, context)
                results.append(result)
            except Exception as e:
                print(f"  [runner] Action {action.action_type} failed: {e}", file=sys.stderr)
                results.append(ActionResult(
                    action_type=action.action_type,
                    status="failed",
                    summary=f"Action failed: {e}",
                    findings=[],
                    artifacts=[],
                    duration_s=0.0,
                    error=str(e),
                ))
        return results

    def _dispatch_action(self, action: Action, context: dict) -> ActionResult:
        """Route an action to the appropriate handler."""
        # Check custom actions first (may be added after construction)
        handler = self.custom_actions.get(action.action_type)
        if not handler:
            handler = self._actions.get(action.action_type)
        if handler:
            return handler.execute(action, context)

        # Check for method-based handlers: execute_<action_type>
        method_name = f"execute_{action.action_type}"
        method = getattr(self, method_name, None)
        if method:
            return method(action, context)

        raise ValueError(
            f"Unknown action type '{action.action_type}'. "
            f"Register it via custom_actions or define execute_{action.action_type}()."
        )

    # ------------------------------------------------------------------
    # Phase 4: Summarize
    # ------------------------------------------------------------------

    def summarize(self, context: dict, results: list[ActionResult]) -> dict:
        """Merge action results into updated status.

        Default: collect all findings, update status fields.
        Override for custom synthesis (LLM-backed hypothesis updates, etc.)
        """
        status = dict(context.get("status", {}))
        all_findings = []
        all_errors = []

        for r in results:
            all_findings.extend(r.findings)
            if r.error:
                all_errors.append({"action": r.action_type, "error": r.error})

        # Append findings to key_findings
        existing = status.get("key_findings", [])
        cycle = context["meta"].get("cycle", 0) + 1
        for f in all_findings:
            existing.append({
                "finding": f.get("finding", ""),
                "provenance": f.get("source", ""),
                "relevance_score": f.get("relevance", 3),
                "added_cycle": cycle,
            })

        # Cap at 10 most recent
        status["key_findings"] = existing[-10:]
        status["cycle_number"] = cycle

        return status

    # ------------------------------------------------------------------
    # Phase 5: Report
    # ------------------------------------------------------------------

    def report(self, context: dict, results: list[ActionResult]):
        """Write summary.md + summary.json for the orchestrator digest."""
        self.summary_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        started_at = context.get("_started_at", datetime.now().isoformat())

        # Determine overall status
        statuses = [r.status for r in results]
        if all(s == "success" for s in statuses):
            overall = "success"
        elif all(s in ("failed", "skipped") for s in statuses):
            overall = "failed"
        else:
            overall = "partial"

        # Collect
        all_findings = []
        all_artifacts = []
        all_errors = []
        total_duration = 0.0
        summaries = []

        for r in results:
            all_findings.extend(r.findings)
            all_artifacts.extend(r.artifacts)
            if r.error:
                all_errors.append({"type": r.action_type, "message": r.error})
            total_duration += r.duration_s
            summaries.append(r.summary)

        tldr = summaries[:3] if summaries else [f"Cycle completed ({overall})"]

        summary_json = {
            "task_id": self.task_id,
            "date": date_str,
            "status": overall,
            "tldr": tldr,
            "action_items": [],
            "artifacts": all_artifacts,
            "errors": all_errors,
            "metadata": {
                "started_at": started_at,
                "ended_at": datetime.now().isoformat(),
                "duration_s": round(total_duration, 1),
                "budget_hint": "low",
                "actions_run": len(results),
            },
        }

        # summary.json
        with open(self.summary_dir / "summary.json", "w") as f:
            json.dump(summary_json, f, indent=2, ensure_ascii=False)

        # summary.md
        md_lines = [
            f"# {self.task_id} — {date_str}",
            "",
            "## TL;DR",
        ]
        for item in tldr:
            md_lines.append(f"- {item}")
        md_lines.append("")

        if all_findings:
            md_lines.append("## Findings")
            for f in all_findings[:5]:
                md_lines.append(f"- {f.get('finding', '')[:200]}")
            md_lines.append("")

        if all_artifacts:
            md_lines.append("## Artifacts")
            for a in all_artifacts:
                md_lines.append(f"- `{a.get('path', '')}` — {a.get('description', '')}")
            md_lines.append("")

        if all_errors:
            md_lines.append("## Errors")
            for e in all_errors:
                md_lines.append(f"- [{e['type']}] {e['message']}")
            md_lines.append("")

        md_lines.append("## Run Metadata")
        md_lines.append(f"- Duration: {total_duration:.1f}s")
        md_lines.append(f"- Actions: {len(results)}")
        md_lines.append("")

        with open(self.summary_dir / "summary.md", "w") as f:
            f.write("\n".join(md_lines))

    # ------------------------------------------------------------------
    # Phase 6: Save State
    # ------------------------------------------------------------------

    def save_state(self, context: dict, results: list[ActionResult]):
        """Atomic state persistence: update meta, append to log."""
        meta = context["meta"]
        meta["cycle"] = meta.get("cycle", 0) + 1
        meta["last_cycle_time"] = datetime.now().isoformat()

        # Save task_state.json
        state_file = self.state_dir / "task_state.json"
        tmp = state_file.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        tmp.replace(state_file)

        # Save updated status.yaml (if summarize produced one)
        if "_updated_status" in context:
            status_file = self.state_dir / "status.yaml"
            tmp = status_file.with_suffix(".yaml.tmp")
            with open(tmp, "w") as f:
                yaml.dump(context["_updated_status"], f,
                          default_flow_style=False, allow_unicode=True)
            tmp.replace(status_file)

        # Append to exploration_log.jsonl
        log_entries = []
        cycle = meta["cycle"]
        timestamp = datetime.now().isoformat()
        for r in results:
            log_entries.append({
                "cycle": cycle,
                "timestamp": timestamp,
                "action_type": r.action_type,
                "status": r.status,
                "summary": r.summary[:200],
                "findings_count": len(r.findings),
                "duration_s": r.duration_s,
                "error": r.error,
            })

        if log_entries:
            log_path = self.state_dir / "exploration_log.jsonl"
            with open(log_path, "a") as f:
                for entry in log_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Full cycle
    # ------------------------------------------------------------------

    def run_cycle(self):
        """Execute one complete cycle: load → plan → execute → summarize → report → save."""
        started = time.time()
        started_iso = datetime.now().isoformat()

        print(f"[{self.task_id}] Starting cycle", file=sys.stderr)

        # Phase 1: Load
        print(f"  Phase 1: Load state", file=sys.stderr)
        context = self.load_state()
        context["_started_at"] = started_iso

        # Phase 2: Plan
        print(f"  Phase 2: Plan actions", file=sys.stderr)
        actions = self.plan(context)
        print(f"  Planned {len(actions)} action(s): "
              f"{[a.action_type for a in actions]}", file=sys.stderr)

        # Phase 3: Execute
        print(f"  Phase 3: Execute actions", file=sys.stderr)
        results = self.execute(actions, context)
        for r in results:
            status_str = r.status
            if r.error:
                status_str += f" ({r.error[:60]})"
            print(f"    {r.action_type}: {status_str} ({r.duration_s:.1f}s)",
                  file=sys.stderr)

        # Phase 4: Summarize
        print(f"  Phase 4: Summarize", file=sys.stderr)
        updated_status = self.summarize(context, results)
        context["_updated_status"] = updated_status

        # Phase 5: Report
        print(f"  Phase 5: Report", file=sys.stderr)
        self.report(context, results)

        # Phase 6: Save
        print(f"  Phase 6: Save state", file=sys.stderr)
        self.save_state(context, results)

        elapsed = time.time() - started
        success_count = sum(1 for r in results if r.status == "success")
        print(f"[{self.task_id}] Cycle complete ({elapsed:.1f}s, "
              f"{success_count}/{len(results)} actions succeeded)",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Run a cycle from command line."""
    import argparse
    parser = argparse.ArgumentParser(description="CycleRunner — generic task runner")
    parser.add_argument("--definition", required=True, help="Path to definition.yaml")
    parser.add_argument("--state", required=True, help="Path to state directory")
    parser.add_argument("--summary-dir", default=None, help="Path to write summaries")
    parser.add_argument("--task-id", default=None, help="Override task ID")
    args = parser.parse_args()

    runner = CycleRunner(
        definition=args.definition,
        state_dir=args.state,
        summary_dir=args.summary_dir,
        task_id=args.task_id,
    )
    runner.run_cycle()


if __name__ == "__main__":
    main()
