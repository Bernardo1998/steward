"""Guardrails G1–G10 — auditable functions.

Programmatic: G2, G3, G5, G7, G8 (structural), G9
LLM-backed:   G1, G4, G6, G10
"""

import json
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Optional

from .llm import call_llm_json


@dataclass
class GuardrailResult:
    guardrail: str
    passed: bool
    details: str
    action_taken: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# G1 — Relevance Gate (LLM)
# ---------------------------------------------------------------------------

def g1_relevance_gate(claims: list[dict], project_def: dict) -> list[GuardrailResult]:
    """Score claims for relevance to project goal. Returns one result per claim."""
    if not claims:
        return []

    goal = project_def.get("goal", "")
    scope_in = project_def.get("scope_boundaries", {}).get("in_scope", [])
    lang_instructions = project_def.get("output_language_instructions", "")

    claims_text = "\n".join(
        f"- Claim {i+1}: {c.get('claim', c.get('finding', ''))}"
        for i, c in enumerate(claims)
    )

    lang_note = ""
    if lang_instructions:
        lang_note = f"\nLANGUAGE: {lang_instructions}\nWrite justifications in the project's output language.\n"

    prompt = f"""You are a research relevance evaluator.

PROJECT GOAL: {goal}
IN-SCOPE TOPICS: {', '.join(scope_in)}
{lang_note}
CLAIMS TO EVALUATE:
{claims_text}

For each claim, score relevance 1-5 (5=directly advances goal, 1=unrelated).
Explain in ≤2 sentences how each advances the goal (or why it doesn't).

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "evaluations": [
    {{
      "claim_index": 1,
      "relevance_score": 4,
      "justification": "This directly relates to..."
    }}
  ]
}}"""

    try:
        result = call_llm_json(prompt, timeout=120)
        evaluations = result.get("evaluations", [])
    except Exception as e:
        # On LLM failure, pass all claims (fail-open for research)
        return [
            GuardrailResult("G1", True, f"LLM unavailable, auto-passed: {e}", "auto_passed")
            for _ in claims
        ]

    results = []
    eval_map = {e.get("claim_index", i+1): e for i, e in enumerate(evaluations)}
    for i, claim in enumerate(claims):
        ev = eval_map.get(i + 1, {})
        score = ev.get("relevance_score", 3)
        justification = ev.get("justification", "")
        passed = score >= 3
        action = "kept" if passed else "discarded"
        results.append(GuardrailResult(
            "G1", passed,
            f"Score {score}/5: {justification}",
            action,
        ))
    return results


# ---------------------------------------------------------------------------
# G2 — Deduplication Gate (programmatic)
# ---------------------------------------------------------------------------

def g2_dedup_gate(
    query: str,
    log_entries: list[dict],
    current_cycle: int,
    lookback_cycles: int = 3,
    threshold: float = 0.75,
) -> GuardrailResult:
    """Check if query is too similar to recent exploration log entries."""
    recent = [
        e for e in log_entries
        if e.get("cycle", 0) >= current_cycle - lookback_cycles
    ]
    for entry in recent:
        past_query = entry.get("query", "")
        similarity = SequenceMatcher(None, query.lower(), past_query.lower()).ratio()
        if similarity >= threshold:
            return GuardrailResult(
                "G2", False,
                f"Similar to cycle {entry.get('cycle')} query (similarity={similarity:.2f}): '{past_query[:60]}'",
                "skipped_duplicate",
            )
    return GuardrailResult("G2", True, "No duplicates found", "proceeded")


# ---------------------------------------------------------------------------
# G3 — Provenance Check (programmatic)
# ---------------------------------------------------------------------------

def g3_provenance_check(claims: list[dict]) -> list[GuardrailResult]:
    """Check that claims have source provenance."""
    results = []
    for claim in claims:
        sources = claim.get("sources", claim.get("supporting_sources", []))
        source_url = claim.get("source", claim.get("url_or_doi", ""))
        has_provenance = bool(sources) or bool(source_url)
        if has_provenance:
            results.append(GuardrailResult("G3", True, "Has provenance", "verified"))
        else:
            results.append(GuardrailResult(
                "G3", False,
                "No source URL/DOI — labeled [HYPOTHESIS]",
                "labeled_hypothesis",
            ))
    return results


# ---------------------------------------------------------------------------
# G4 — Novelty Check (LLM)
# ---------------------------------------------------------------------------

def g4_novelty_check(
    suggestions: list[dict],
    prev_suggestions: list[dict],
) -> GuardrailResult:
    """Check that at least one suggestion is genuinely novel."""
    if not prev_suggestions:
        return GuardrailResult("G4", True, "First cycle — all suggestions are novel", "passed")

    current_text = "\n".join(
        f"- {s.get('action', s.get('text', ''))}" for s in suggestions
    )
    prev_text = "\n".join(
        f"- {s.get('action', s.get('text', ''))}" for s in prev_suggestions
    )

    prompt = f"""Compare these two lists of action suggestions.

PREVIOUS CYCLE:
{prev_text}

CURRENT CYCLE:
{current_text}

Are any of the current suggestions genuinely novel (not just rewordings)?

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "novel_count": 2,
  "novel_indices": [1, 3],
  "justification": "Suggestions 1 and 3 propose new directions..."
}}"""

    try:
        result = call_llm_json(prompt, timeout=90)
        novel_count = result.get("novel_count", 0)
        if novel_count > 0:
            return GuardrailResult(
                "G4", True,
                f"{novel_count} novel suggestions: {result.get('justification', '')}",
                "passed",
            )
        return GuardrailResult(
            "G4", False,
            "No novel suggestions — needs_human_input set to true",
            "flagged_stale",
        )
    except Exception as e:
        return GuardrailResult("G4", True, f"LLM unavailable, auto-passed: {e}", "auto_passed")


# ---------------------------------------------------------------------------
# G5 — Size Cap (programmatic)
# ---------------------------------------------------------------------------

def g5_size_cap(
    items: list[dict],
    max_size: int,
    sort_key: str = "relevance_score",
    age_key: str = "added_cycle",
) -> tuple[list[dict], list[GuardrailResult]]:
    """Enforce size cap on a list. Returns (kept_items, eviction_results)."""
    if len(items) <= max_size:
        return items, []

    # Sort: highest relevance first, then newest first
    sorted_items = sorted(
        items,
        key=lambda x: (x.get(sort_key, 0), x.get(age_key, 0)),
        reverse=True,
    )
    kept = sorted_items[:max_size]
    evicted = sorted_items[max_size:]

    results = []
    for item in evicted:
        desc = item.get("finding", item.get("question", item.get("action", "?")))[:60]
        results.append(GuardrailResult(
            "G5", True,
            f"Evicted (score={item.get(sort_key, '?')}, cycle={item.get(age_key, '?')}): {desc}",
            "evicted",
        ))
    return kept, results


# ---------------------------------------------------------------------------
# G6 — Self-Review (LLM)
# ---------------------------------------------------------------------------

def g6_self_review(status: dict, project_def: dict) -> tuple[list[str], GuardrailResult]:
    """Pre-send self-review. Returns (issues_found, result)."""
    lang_instructions = project_def.get("output_language_instructions", "")
    lang_note = f"\nLANGUAGE: {lang_instructions}\n" if lang_instructions else ""

    prompt = f"""You are reviewing a research status report before sending it to the human.

PROJECT GOAL: {project_def.get('goal', '')}
{lang_note}

STATUS REPORT:
- Hypothesis: {status.get('current_hypothesis', '')}
- Confidence: {status.get('confidence_score', '?')}/5
- Open questions: {json.dumps(status.get('open_questions', []), indent=2)}
- Key findings: {json.dumps(status.get('key_findings', []), indent=2)}
- Action suggestions: {json.dumps(status.get('action_suggestions', []), indent=2)}

Check:
1. Is anything unclear or missing?
2. Does the confidence score match the evidence strength?
3. Are action suggestions specific enough to act on?

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "issues": ["issue 1", "issue 2"],
  "confidence_appropriate": true,
  "suggestions_actionable": true,
  "overall_quality": "good"
}}"""

    try:
        result = call_llm_json(prompt, timeout=90)
        issues = result.get("issues", [])
        passed = len(issues) == 0
        return issues, GuardrailResult(
            "G6", passed,
            f"{len(issues)} issues found" + (f": {'; '.join(issues[:3])}" if issues else ""),
            "reviewed",
        )
    except Exception as e:
        return [], GuardrailResult("G6", True, f"LLM unavailable: {e}", "auto_passed")


# ---------------------------------------------------------------------------
# G7 — Stagnation Detection (programmatic)
# ---------------------------------------------------------------------------

def g7_stagnation_check(metrics_history: list[dict]) -> GuardrailResult:
    """Check if research is stagnating (2+ flat cycles)."""
    if len(metrics_history) < 3:
        return GuardrailResult("G7", True, f"Only {len(metrics_history)} cycles — too early", "passed")

    recent = metrics_history[-2:]
    all_flat = True
    details = []
    for m in recent:
        new_sources = m.get("new_sources", 0)
        new_claims = m.get("new_claims", 0)
        conf_delta = m.get("confidence_delta", 0)
        if new_sources > 0 or new_claims > 0 or abs(conf_delta) > 0:
            all_flat = False
        details.append(f"cycle {m.get('cycle', '?')}: sources={new_sources}, claims={new_claims}, Δconf={conf_delta}")

    if all_flat:
        return GuardrailResult(
            "G7", False,
            f"Stagnant for 2 cycles: {'; '.join(details)}",
            "needs_human_input",
        )
    return GuardrailResult("G7", True, f"Progress detected: {details[-1]}", "passed")


# ---------------------------------------------------------------------------
# G9 — Speculative Isolation (programmatic)
# ---------------------------------------------------------------------------

def g9_validate_isolation(buffer_items: list[dict], status_findings: list[dict]) -> GuardrailResult:
    """Ensure speculative buffer items haven't leaked into main status."""
    buffer_texts = {item.get("direction", "") for item in buffer_items}
    finding_texts = {f.get("finding", "") for f in status_findings}
    leaked = buffer_texts & finding_texts
    if leaked:
        return GuardrailResult(
            "G9", False,
            f"{len(leaked)} speculative items leaked into main status",
            "isolation_violated",
        )
    return GuardrailResult("G9", True, "Buffer properly isolated", "passed")


# ---------------------------------------------------------------------------
# G10 — Feedback Integration (LLM)
# ---------------------------------------------------------------------------

def g10_parse_feedback(
    reply_text: str,
    status: dict,
    project_def: dict,
) -> dict:
    """Parse human reply and extract structured feedback.

    Returns dict with:
        corrections: list of {finding, correction}
        rejected_suggestions: list of str
        commands: list of str ("pause", "done", "resume", etc.)
        new_priorities: list of str
        raw_reply: str
    """
    prompt = f"""Parse this human reply to a research report.

PROJECT: {project_def.get('goal', '')[:200]}

CURRENT ACTION SUGGESTIONS:
{json.dumps([s.get('action', '') for s in status.get('action_suggestions', [])], indent=2)}

HUMAN REPLY:
{reply_text}

Extract structured feedback. The human may:
- Correct a finding ("actually X is wrong, it should be Y")
- Reject a suggestion ("don't pursue X")
- Give a command ("pause", "done", "resume", "focus on X")
- Add new priorities ("I also want to look at Y")

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "corrections": [{{"original": "...", "correction": "..."}}],
  "rejected_suggestions": ["suggestion text that was rejected"],
  "commands": ["pause"],
  "new_priorities": ["new topic or direction"],
  "summary": "One sentence summary of what the human wants"
}}"""

    try:
        result = call_llm_json(prompt, timeout=90)
        result["raw_reply"] = reply_text
        return result
    except Exception as e:
        return {
            "corrections": [],
            "rejected_suggestions": [],
            "commands": [],
            "new_priorities": [],
            "raw_reply": reply_text,
            "parse_error": str(e),
        }
