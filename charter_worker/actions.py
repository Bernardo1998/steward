"""Action types and the universal ActionResult format.

Every action — web search, experiment, code execution, custom — returns
an ActionResult. The runner's summarize phase is action-agnostic.
"""

import time
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class ActionResult:
    """Universal return format for all actions.

    findings: structured results for synthesis (hypothesis update, guardrails).
    artifacts: files/outputs produced (listed in summary and digest).
    summary: human-readable — goes directly into email report.
    """
    action_type: str
    status: str                             # "success" | "failed" | "partial" | "skipped"
    summary: str                            # 1-2 sentence: what was done + outcome
    findings: list[dict] = field(default_factory=list)
        # Each: {finding: str, source: str, relevance: int (optional)}
    artifacts: list[dict] = field(default_factory=list)
        # Each: {path: str, description: str, type: str}
        # type: "data" | "code" | "report" | "figure"
    duration_s: float = 0.0
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Action:
    """Task descriptor: what action to run and with what config."""
    action_type: str
    query: Optional[str] = None             # for search-type actions
    config: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Built-in action: web_search (deep research fan-out/fan-in)
# ---------------------------------------------------------------------------

class WebSearchAction:
    """Deep web search using the fan-out/fan-in research engine."""
    action_type = "web_search"

    def execute(self, action: Action, context: dict) -> ActionResult:
        started = time.time()
        query = action.query or context.get("definition", {}).get("goal", "")
        timeout = action.config.get("timeout", 900)
        max_workers = action.config.get("max_workers", 2)

        try:
            from .search.engine import run_research
            result = run_research(
                query,
                context=context.get("definition", {}).get("goal", ""),
                max_workers=max_workers,
                worker_timeout=min(timeout // 2, 600),
                planner_timeout=min(timeout // 3, 300),
                aggregator_timeout=min(timeout // 3, 300),
                reviewer_timeout=min(timeout // 3, 300),
            )

            synthesis = result.get("synthesis", {})
            sources = result.get("sources", [])
            key_findings = synthesis.get("key_findings", [])
            if isinstance(key_findings, list):
                findings = [
                    {"finding": f if isinstance(f, str) else str(f), "source": "web_search"}
                    for f in key_findings
                ]
            else:
                findings = []

            summary_text = synthesis.get("executive_summary", f"Searched for: {query[:80]}")

            return ActionResult(
                action_type="web_search",
                status="success",
                summary=summary_text[:300],
                findings=findings,
                artifacts=[],
                duration_s=round(time.time() - started, 1),
                metadata={"query": query[:200], "sources_found": len(sources)},
            )

        except Exception as e:
            return ActionResult(
                action_type="web_search",
                status="failed",
                summary=f"Web search failed: {e}",
                findings=[],
                artifacts=[],
                duration_s=round(time.time() - started, 1),
                error=str(e),
                metadata={"query": query[:200]},
            )


# ---------------------------------------------------------------------------
# Built-in action: lightweight_search (single LLM search call)
# ---------------------------------------------------------------------------

class LightweightSearchAction:
    """Quick search using call_llm(search=True)."""
    action_type = "lightweight_search"

    def execute(self, action: Action, context: dict) -> ActionResult:
        started = time.time()
        query = action.query or ""
        timeout = action.config.get("timeout", 120)

        try:
            from .llm import call_llm
            output = call_llm(query, search=True, timeout=timeout)

            findings = [{
                "finding": output[:500],
                "source": "lightweight_search",
            }]

            return ActionResult(
                action_type="lightweight_search",
                status="success",
                summary=f"Searched: {query[:80]}",
                findings=findings,
                artifacts=[],
                duration_s=round(time.time() - started, 1),
                metadata={"query": query[:200]},
            )

        except Exception as e:
            return ActionResult(
                action_type="lightweight_search",
                status="failed",
                summary=f"Lightweight search failed: {e}",
                findings=[],
                artifacts=[],
                duration_s=round(time.time() - started, 1),
                error=str(e),
            )


# ---------------------------------------------------------------------------
# Built-in action: experiment (plan → write code → run → parse results)
# ---------------------------------------------------------------------------

class ExperimentAction:
    """Plan an experiment step via LLM, write code, execute, parse results.

    Config keys (via Action.config):
        repo: str          — path to experiment repo (required)
        timeout: int       — max seconds for experiment execution (default: 600)
        planning_timeout: int — max seconds for LLM planning step (default: 300)
    """
    action_type = "experiment"

    def execute(self, action: Action, context: dict) -> ActionResult:
        import json as _json
        import subprocess as _sp
        started = time.time()
        config = action.config
        repo_path = config.get("repo")
        if not repo_path:
            return ActionResult(
                action_type="experiment", status="failed",
                summary="No experiment repo path configured",
                findings=[], artifacts=[], duration_s=0.0,
                error="config.repo is required",
            )

        repo = Path(repo_path)
        repo.mkdir(parents=True, exist_ok=True)

        exec_timeout = config.get("timeout", 600)
        plan_timeout = config.get("planning_timeout", 300)

        # Build planning prompt from context
        goal = context.get("definition", {}).get("goal", "")
        hypothesis = context.get("status", {}).get("current_hypothesis", "")
        suggestion = action.query or config.get("suggestion", "Run next experiment step")

        plan_prompt = f"""You are an experiment planner.

RESEARCH GOAL: {goal[:400]}
HYPOTHESIS: {hypothesis[:200]}
ACTION: {suggestion[:300]}

Design the SMALLEST concrete experiment step. One self-contained Python script,
≤200 lines, runnable with `python` in under 10 minutes.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "step_id": "short_snake_case_id",
  "description": "One sentence",
  "code": "full Python source code",
  "filename": "experiments/step_name.py",
  "run_command": "python experiments/step_name.py",
  "expected_outputs": ["results/step_name.json"]
}}"""

        try:
            from .llm import call_llm_json
            plan = call_llm_json(plan_prompt, timeout=plan_timeout)
        except Exception as e:
            return ActionResult(
                action_type="experiment", status="failed",
                summary=f"Experiment planning failed: {e}",
                findings=[], artifacts=[], duration_s=round(time.time() - started, 1),
                error=str(e),
            )

        # Write code to repo
        step_id = plan.get("step_id", "unknown_step")
        filename = plan.get("filename", f"experiments/{step_id}.py")
        code = plan.get("code", "")
        run_cmd = plan.get("run_command", f"python {filename}")

        if not code:
            return ActionResult(
                action_type="experiment", status="failed",
                summary="LLM produced empty code",
                findings=[], artifacts=[], duration_s=round(time.time() - started, 1),
                error="Empty code from planner",
            )

        code_path = repo / filename
        code_path.parent.mkdir(parents=True, exist_ok=True)
        code_path.write_text(code)

        # Execute
        try:
            proc = _sp.run(
                run_cmd, shell=True,
                cwd=str(repo),
                capture_output=True, text=True,
                timeout=exec_timeout,
            )
            exec_status = "success" if proc.returncode == 0 else "failed"
            exec_output = proc.stdout[-2000:] if proc.stdout else ""
            exec_error = proc.stderr[-1000:] if proc.stderr else ""
        except _sp.TimeoutExpired:
            exec_status = "failed"
            exec_output = ""
            exec_error = f"Experiment timed out after {exec_timeout}s"
        except Exception as e:
            exec_status = "failed"
            exec_output = ""
            exec_error = str(e)

        # Collect artifacts
        artifacts = [{"path": str(code_path), "description": plan.get("description", ""), "type": "code"}]
        for expected in plan.get("expected_outputs", []):
            out_path = repo / expected
            if out_path.exists():
                artifacts.append({"path": str(out_path), "description": f"Output: {expected}", "type": "data"})

        # Parse findings from output
        findings = []
        if exec_status == "success" and exec_output:
            findings.append({
                "finding": f"[Experiment {step_id}] {plan.get('description', '')}: {exec_output[:200]}",
                "source": f"experiment:{step_id}",
            })

        summary_text = f"Experiment '{step_id}': {exec_status}"
        if exec_status == "success":
            summary_text += f" — {plan.get('description', '')}"
        else:
            summary_text += f" — {exec_error[:100]}"

        return ActionResult(
            action_type="experiment",
            status=exec_status,
            summary=summary_text[:300],
            findings=findings,
            artifacts=artifacts,
            duration_s=round(time.time() - started, 1),
            error=exec_error if exec_status != "success" else None,
            metadata={"step_id": step_id, "run_command": run_cmd},
        )


# ---------------------------------------------------------------------------
# Registry of built-in actions
# ---------------------------------------------------------------------------

BUILT_IN_ACTIONS = {
    "web_search": WebSearchAction(),
    "lightweight_search": LightweightSearchAction(),
    "experiment": ExperimentAction(),
}
