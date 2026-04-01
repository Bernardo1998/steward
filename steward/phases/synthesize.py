"""Phase 3 — Synthesize & Reflect.

Three passes: extraction, contradiction/gap, relevance.
Updates the rolling status document.
Archives evicted items to cycle_archive.jsonl.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..llm import call_llm_json
from .guardrails import (
    g1_relevance_gate,
    g3_provenance_check,
    g4_novelty_check,
    g5_size_cap,
    GuardrailResult,
)


def _load_archive_summaries(project_dir: Path, max_entries: int = 10) -> str:
    """Load compressed summaries from cycle_archive.jsonl for synthesis context."""
    archive_path = project_dir / "cycle_archive.jsonl"
    if not archive_path.exists():
        return ""

    entries = []
    with open(archive_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not entries:
        return ""

    # Take most recent N entries
    recent = entries[-max_entries:]
    lines = []
    for e in recent:
        cycle = e.get("cycle", "?")
        hyp = e.get("hypothesis", "")[:80]
        evicted_count = len(e.get("evicted_findings", []))
        conf = e.get("confidence", "?")
        feedback_summary = e.get("feedback_summary", "")
        line = f"  Cycle {cycle} (conf {conf}/5): {hyp}"
        if evicted_count:
            line += f" [evicted {evicted_count} findings]"
        if feedback_summary:
            line += f" [feedback: {feedback_summary[:60]}]"
        lines.append(line)

    return "\n".join(lines)


def _append_archive(project_dir: Path, archive_entry: dict):
    """Append one entry to cycle_archive.jsonl."""
    archive_path = project_dir / "cycle_archive.jsonl"
    with open(archive_path, "a") as f:
        f.write(json.dumps(archive_entry, ensure_ascii=False) + "\n")


def _run_synthesis_llm(
    raw_findings: list[dict],
    status: dict,
    definition: dict,
    promoted_findings: list[dict],
    archive_summary: str = "",
    context_files_summary: str = "",
) -> dict:
    """LLM call to synthesize findings into status update."""
    goal = definition.get("goal", "")
    current_hyp = status.get("current_hypothesis", "No hypothesis yet.")
    existing_findings = status.get("key_findings", [])

    # Combine raw + promoted findings
    all_new = raw_findings + promoted_findings
    findings_text = json.dumps(all_new[:20], indent=2)  # Cap to avoid prompt overflow
    existing_text = json.dumps(existing_findings[:10], indent=2)

    # Build language directive if project specifies output_language
    lang_directive = ""
    lang_instructions = definition.get("output_language_instructions", "")
    if definition.get("output_language") == "chinese" or lang_instructions:
        lang_directive = f"""
LANGUAGE REQUIREMENT (MANDATORY):
{lang_instructions}
All text values in the JSON output (hypothesis, claims, gaps, actions, rationale,
questions) MUST be written in Chinese (简体中文). For medical terms, on FIRST use
write the full Chinese name followed by the English abbreviation in parentheses,
e.g. "乙型肝炎病毒（HBV）". After first use, abbreviation alone is fine.
JSON keys and source URLs remain in English.
"""

    # Build archive context
    archive_section = ""
    if archive_summary:
        archive_section = f"""
PRIOR CYCLE HISTORY (compressed — do not repeat old work, build on it):
{archive_summary}
"""

    # Build context files section
    context_files_section = ""
    if context_files_summary:
        context_files_section = f"""
PROJECT CONTEXT FILES (provided by the human — use to ground your analysis):
{context_files_summary}
"""

    prompt = f"""You are a research synthesizer. Given new findings and existing status,
produce an updated research status.

PROJECT GOAL: {goal}
{lang_directive}{archive_section}{context_files_section}
CURRENT HYPOTHESIS: {current_hyp}

EXISTING KEY FINDINGS:
{existing_text}

NEW RAW FINDINGS:
{findings_text}

Instructions:
1. Extract discrete claims from new findings. Each must reference its source.
2. Cross-reference with existing findings. Flag contradictions.
3. Identify gaps (unanswered questions).
4. Update the hypothesis based on all evidence.
5. Suggest 3-5 next actions (at least one must be novel if possible).
6. Score confidence 1-5 on overall project progress.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "current_hypothesis": "Updated 1-3 sentence hypothesis",
  "extracted_claims": [
    {{
      "claim": "A specific finding",
      "source": "URL or reference",
      "confidence": "high|medium|low"
    }}
  ],
  "contradictions": [
    {{
      "existing": "What we thought before",
      "new_evidence": "What new evidence shows",
      "resolution": "How to reconcile"
    }}
  ],
  "gaps": ["Unanswered question 1", "Unanswered question 2"],
  "action_suggestions": [
    {{
      "action": "Specific actionable suggestion",
      "type": "research|experiment|human_decision",
      "novel": true,
      "rationale": "Why this action"
    }}
  ],
  "confidence_score": 3,
  "needs_human_input": false,
  "human_input_questions": []
}}"""

    # Allow definition-level override, otherwise longer for bilingual prompts
    timeout = definition.get("synthesis_timeout") or (300 if definition.get("output_language") else 180)
    return call_llm_json(prompt, timeout=timeout)


def synthesize(
    context: dict,
    raw_findings: list[dict],
    project_dir: Path = None,
) -> tuple[dict, list[GuardrailResult]]:
    """Run synthesis phase.

    Args:
        context: From phase_context.load_context()
        raw_findings: From phase_research.research()
        project_dir: Path to project dir (for archive + context files)

    Returns:
        (updated_status, guardrail_results)
    """
    definition = context["definition"]
    status = context["status"]
    promoted_findings = context.get("promoted_findings", [])
    current_cycle = status.get("cycle_number", 0) + 1
    prev_suggestions = status.get("action_suggestions", [])
    prev_hypothesis = status.get("current_hypothesis", "")
    guardrail_results = []

    # Load archive summaries for context
    archive_summary = ""
    if project_dir:
        archive_summary = _load_archive_summaries(project_dir)

    # Load context files summary
    context_files_summary = context.get("context_files_summary", "")

    # If no new findings and no promoted, minimal update
    if not raw_findings and not promoted_findings:
        print("  [phase3] No new findings to synthesize", file=sys.stderr)
        status["cycle_number"] = current_cycle
        return status, [GuardrailResult("G1", True, "No findings to evaluate", "skipped")]

    # Run LLM synthesis
    print("  [phase3] Running synthesis...", file=sys.stderr)
    try:
        synthesis = _run_synthesis_llm(
            raw_findings, status, definition, promoted_findings,
            archive_summary=archive_summary,
            context_files_summary=context_files_summary,
        )
    except Exception as e:
        print(f"  [phase3] Synthesis LLM failed: {e}", file=sys.stderr)
        status["cycle_number"] = current_cycle
        return status, [GuardrailResult("G1", False, f"Synthesis failed: {e}", "error")]

    # --- Pass 1: G3 Provenance Check ---
    claims = synthesis.get("extracted_claims", [])
    g3_results = g3_provenance_check(claims)
    guardrail_results.extend(g3_results)

    # Label claims without provenance
    for i, (claim, g3r) in enumerate(zip(claims, g3_results)):
        if not g3r.passed:
            claims[i]["claim"] = f"[HYPOTHESIS] {claim.get('claim', '')}"

    # --- Pass 2: G1 Relevance Gate ---
    if claims:
        g1_results = g1_relevance_gate(claims, definition)
        guardrail_results.extend(g1_results)

        # Filter claims by relevance
        kept_claims = []
        discarded = 0
        for claim, g1r in zip(claims, g1_results):
            if g1r.passed:
                kept_claims.append(claim)
            else:
                discarded += 1

        # Drift warning if >30% discarded
        if len(claims) > 0 and discarded / len(claims) > 0.3:
            drift_msg = f"Drift warning: {discarded}/{len(claims)} findings irrelevant"
            print(f"  [phase3] {drift_msg}", file=sys.stderr)
            if "drift_warnings" not in status:
                status["drift_warnings"] = []
            status["drift_warnings"].append(drift_msg)
    else:
        kept_claims = []

    # --- Update status document ---

    # Update hypothesis
    status["current_hypothesis"] = synthesis.get(
        "current_hypothesis", status.get("current_hypothesis", "")
    )

    # Merge findings
    existing_findings = status.get("key_findings", [])
    for claim in kept_claims:
        existing_findings.append({
            "finding": claim.get("claim", ""),
            "provenance": claim.get("source", ""),
            "relevance_score": 4,  # Passed G1, so at least 3
            "added_cycle": current_cycle,
        })

    # G5: Size cap on findings
    existing_findings, g5_results = g5_size_cap(existing_findings, 10)
    guardrail_results.extend(g5_results)
    status["key_findings"] = existing_findings

    # Update open questions from gaps
    existing_questions = status.get("open_questions", [])
    for gap in synthesis.get("gaps", []):
        existing_questions.append({
            "question": gap,
            "priority": "medium",
            "added_cycle": current_cycle,
        })
    existing_questions, g5q_results = g5_size_cap(
        existing_questions, 5, sort_key="priority", age_key="added_cycle"
    )
    # Priority sort needs special handling (high > medium > low)
    priority_order = {"high": 3, "medium": 2, "low": 1}
    existing_questions.sort(
        key=lambda x: priority_order.get(x.get("priority", "medium"), 2),
        reverse=True,
    )
    existing_questions = existing_questions[:5]
    guardrail_results.extend(g5q_results)
    status["open_questions"] = existing_questions

    # Update action suggestions
    new_suggestions = synthesis.get("action_suggestions", [])

    # G4: Novelty check
    g4_result = g4_novelty_check(new_suggestions, prev_suggestions)
    guardrail_results.append(g4_result)
    if not g4_result.passed:
        synthesis["needs_human_input"] = True
        if "human_input_questions" not in synthesis:
            synthesis["human_input_questions"] = []
        synthesis["human_input_questions"].append(
            "All suggestions are repeated from previous cycle. Should I change approach?"
        )

    # Filter suppressed suggestions
    new_suggestions = [
        s for s in new_suggestions
        if s.get("suppressed_until_cycle", 0) <= current_cycle
    ]
    new_suggestions, g5s_results = g5_size_cap(new_suggestions, 5)
    guardrail_results.extend(g5s_results)
    status["action_suggestions"] = new_suggestions

    # Update confidence + flags
    status["confidence_score"] = synthesis.get("confidence_score", status.get("confidence_score", 3))
    status["needs_human_input"] = synthesis.get(
        "needs_human_input", status.get("needs_human_input", False)
    )
    status["human_input_questions"] = synthesis.get(
        "human_input_questions", status.get("human_input_questions", [])
    )

    # Update cycle number
    status["cycle_number"] = current_cycle

    # Record metrics for G7 stagnation tracking
    cycle_metrics = {
        "cycle": current_cycle,
        "new_sources": len(raw_findings),
        "new_claims": len(kept_claims),
        "confidence_delta": synthesis.get("confidence_score", 3) - status.get("confidence_score", 3),
    }

    status["_cycle_metrics"] = cycle_metrics

    # --- Archive this cycle's evicted items + old hypothesis ---
    if project_dir:
        # Collect all evictions from G5 results
        evicted_findings = [
            r.details for r in guardrail_results
            if r.guardrail == "G5" and r.action_taken == "evicted"
        ]
        evicted_questions = [
            r.details for r in g5q_results
            if r.action_taken == "evicted"
        ]

        archive_entry = {
            "cycle": current_cycle,
            "timestamp": datetime.now().isoformat(),
            "hypothesis": prev_hypothesis[:200],
            "new_hypothesis": status.get("current_hypothesis", "")[:200],
            "confidence": status.get("confidence_score"),
            "new_sources": len(raw_findings),
            "new_claims_kept": len(kept_claims),
            "evicted_findings": evicted_findings,
            "evicted_questions": evicted_questions,
            "feedback_summary": context.get("feedback", {}).get("summary", "") if context.get("feedback") else "",
            "contradictions": synthesis.get("contradictions", []),
        }
        _append_archive(project_dir, archive_entry)
        print(f"  [phase3] Archived cycle {current_cycle}: {len(evicted_findings)} evicted findings, {len(evicted_questions)} evicted questions", file=sys.stderr)

    return status, guardrail_results
