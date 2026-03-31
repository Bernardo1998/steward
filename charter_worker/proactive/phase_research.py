"""Phase 2 — Research & Search.

Generates queries, checks dedup, calls deep_research or lightweight search,
logs everything to exploration log.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .llm import call_llm_json, call_llm
from .guardrails import g2_dedup_gate, GuardrailResult
from ..research.engine import run_research


def generate_queries(
    open_questions: list[dict],
    definition: dict,
) -> list[dict]:
    """Generate search queries from open questions.

    Returns list of {question, query, source_question_idx}.
    """
    if not open_questions:
        # Bootstrap: generate initial questions from definition
        goal = definition.get("goal", "")
        scope = definition.get("scope_boundaries", {}).get("in_scope", [])
        return [{
            "question": goal,
            "query": f"{goal} recent research {' '.join(scope[:2])}",
            "source_question_idx": -1,
        }]

    queries = []
    for i, oq in enumerate(open_questions):
        q_text = oq.get("question", "")
        queries.append({
            "question": q_text,
            "query": q_text,
            "source_question_idx": i,
        })
    return queries


def generate_alternative_queries(
    open_question: str,
    past_queries: list[str],
    definition: dict,
) -> list[str]:
    """Generate reframed queries from different angles (for no-reply refinement)."""
    goal = definition.get("goal", "")
    past_str = "\n".join(f"- {q}" for q in past_queries[-5:])

    prompt = f"""I'm researching: {goal}

For this specific question: "{open_question}"

I've already searched:
{past_str}

Generate 2-3 alternative search queries that approach the same question from different angles:
- Different terminology (academic vs industry)
- Narrower scope (specific sub-problem)
- Broader scope (adjacent field)
- Negation framing ("limitations of X" vs "advantages of X")
- Temporal framing ("recent 2025-2026")

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "queries": ["query 1", "query 2", "query 3"]
}}"""

    try:
        result = call_llm_json(prompt, timeout=60)
        return result.get("queries", [open_question])
    except Exception:
        # Fallback: simple reformulation
        return [f"recent advances in {open_question} 2025 2026"]


def _run_lightweight_search(query: str, timeout: int = 600) -> dict:
    """Run a single lightweight search via codex --search."""
    prompt = f"""Search for recent information about: {query}

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "findings": [
    {{
      "claim": "A specific finding",
      "source": "URL or reference",
      "confidence": "high|medium|low"
    }}
  ],
  "summary": "Brief summary of what was found"
}}"""

    try:
        result = call_llm_json(prompt, search=True, timeout=timeout)
        return result
    except Exception as e:
        return {"findings": [], "summary": f"Search failed: {e}", "error": str(e)}


def _int_cfg(cfg: dict, key: str, default: int, minimum: int = 1) -> int:
    """Parse a positive integer runtime config value with fallback."""
    try:
        value = int(cfg.get(key, default))
    except (AttributeError, TypeError, ValueError):
        value = default
    return max(minimum, value)


def research(
    context: dict,
    budget_tier: str = "full",
    output_dir: Optional[str] = None,
) -> tuple[list[dict], list[GuardrailResult]]:
    """Run research phase.

    Args:
        context: From phase_context.load_context()
        budget_tier: "full" (deep_research) or "light" (quick searches)
        output_dir: Where to store deep_research outputs

    Returns:
        (raw_findings, guardrail_results)
    """
    definition = context["definition"]
    status = context["status"]
    exploration_log = context["exploration_log"]
    current_cycle = status.get("cycle_number", 0) + 1
    days_since_reply = context.get("days_since_reply", 0)
    runtime_cfg = definition.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}
    runtime_override = context.get("_runtime", {})
    if isinstance(runtime_override, dict) and runtime_override:
        runtime_cfg = {**runtime_cfg, **runtime_override}

    open_questions = status.get("open_questions", [])
    guardrail_results = []
    all_findings = []
    new_log_entries = []
    max_queries_per_cycle = _int_cfg(runtime_cfg, "max_queries_per_cycle", 2)
    lightweight_timeout = _int_cfg(runtime_cfg, "lightweight_timeout", 600)
    max_workers = _int_cfg(runtime_cfg, "max_workers", 2)
    max_subquestions = _int_cfg(runtime_cfg, "max_subquestions", 0, minimum=0)
    planner_timeout = _int_cfg(runtime_cfg, "planner_timeout", 600)
    worker_timeout = _int_cfg(runtime_cfg, "worker_timeout", 900)
    aggregator_timeout = _int_cfg(runtime_cfg, "aggregator_timeout", 600)
    reviewer_timeout = _int_cfg(runtime_cfg, "reviewer_timeout", 600)

    # Generate queries
    queries = generate_queries(open_questions, definition)

    # If no reply for >0 days, generate alternative angles
    if days_since_reply > 0 and open_questions:
        past_queries = [e.get("query", "") for e in exploration_log[-10:]]
        for oq in open_questions[:3]:
            alt_queries = generate_alternative_queries(
                oq.get("question", ""), past_queries, definition
            )
            for aq in alt_queries:
                queries.append({
                    "question": oq.get("question", ""),
                    "query": aq,
                    "source_question_idx": -1,
                })

    executed_count = 0

    # Dedup and execute
    for q_info in queries:
        query = q_info["query"]

        # G2: deduplication
        g2_result = g2_dedup_gate(query, exploration_log + new_log_entries, current_cycle)
        guardrail_results.append(g2_result)
        if not g2_result.passed:
            print(f"  [phase2] Skipping duplicate query: {query[:60]}...", file=sys.stderr)
            continue

        if executed_count >= max_queries_per_cycle:
            print(f"  [phase2] Query cap reached ({max_queries_per_cycle}), deferring remaining queries", file=sys.stderr)
            break

        print(f"  [phase2] Researching ({executed_count + 1}/{max_queries_per_cycle} max): {query[:80]}...", file=sys.stderr)
        executed_count += 1
        timestamp = datetime.now().isoformat()

        if budget_tier == "full":
            # Full deep research
            try:
                result = run_research(
                    query,
                    context=definition.get("goal", ""),
                    output_dir=output_dir,
                    max_workers=max_workers,
                    max_subquestions=max_subquestions or None,
                    worker_timeout=worker_timeout,
                    planner_timeout=planner_timeout,
                    aggregator_timeout=aggregator_timeout,
                    reviewer_timeout=reviewer_timeout,
                )
                # Extract findings from deep_research result
                synthesis = result.get("synthesis", {})
                findings = synthesis.get("key_findings", [])
                sources = synthesis.get("all_sources", [])

                all_findings.extend(findings)

                new_log_entries.append({
                    "cycle": current_cycle,
                    "timestamp": timestamp,
                    "query": query,
                    "sources_found": [
                        {"url_or_doi": s, "title": "", "relevance": 3, "summary": ""}
                        for s in sources[:10]
                    ],
                    "conclusion": synthesis.get("direct_answer", "")[:200],
                    "research_grade": result.get("review", {}).get("overall_grade", "?"),
                })
            except Exception as e:
                print(f"  [phase2] Deep research failed: {e}", file=sys.stderr)
                new_log_entries.append({
                    "cycle": current_cycle,
                    "timestamp": timestamp,
                    "query": query,
                    "sources_found": [],
                    "conclusion": f"FAILED: {e}",
                    "error": str(e),
                })
        else:
            # Lightweight search
            result = _run_lightweight_search(query, timeout=lightweight_timeout)
            findings = result.get("findings", [])
            all_findings.extend(findings)

            new_log_entries.append({
                "cycle": current_cycle,
                "timestamp": timestamp,
                "query": query,
                "sources_found": [
                    {"url_or_doi": f.get("source", ""), "title": "", "relevance": 3,
                     "summary": f.get("claim", "")}
                    for f in findings
                ],
                "conclusion": result.get("summary", "")[:200],
            })

    return all_findings, guardrail_results, new_log_entries
