"""Action types and the universal ActionResult format.

Every action — web search, experiment, code execution, custom — returns
an ActionResult. The runner's summarize phase is action-agnostic.
"""

import time
import sys
from dataclasses import dataclass, field, asdict
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
            from .research.engine import run_research
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
            from .proactive.llm import call_llm
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
# Registry of built-in actions
# ---------------------------------------------------------------------------

BUILT_IN_ACTIONS = {
    "web_search": WebSearchAction(),
    "lightweight_search": LightweightSearchAction(),
}
