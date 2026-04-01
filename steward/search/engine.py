#!/usr/bin/env python3
"""
Shared Deep Research Utility — fan-out/fan-in pattern using Codex CLI.

Phases: planner -> parallel workers (with web search) -> aggregator -> reviewer.

Each phase shells out to `codex exec` (OpenAI Codex CLI). The worker phase
uses `--search` (global flag) for live web research.

Usage as library:
    from steward.research.engine import run_research
    result = run_research("What is the state of ...?", output_dir="./out")

CLI: python3 -m steward.research.cli "question"
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------

def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate the Codex CLI process and any descendants it spawned."""
    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return

    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            return

def _run_codex(
    prompt: str,
    *,
    timeout: int = 180,
    search: bool = False,
    model: Optional[str] = None,
) -> str:
    """Run a single `codex exec` call and return stdout.

    Args:
        prompt: The prompt text to send.
        timeout: Subprocess timeout in seconds.
        search: If True, pass ``--search`` as a **global** flag (before ``exec``).
        model: Optional model override (e.g. ``o4-mini``).

    Returns:
        Stripped stdout from the codex process.

    Raises:
        subprocess.TimeoutExpired: If the process exceeds *timeout*.
        RuntimeError: On non-zero exit or other subprocess errors.
    """
    cmd: list[str] = ["codex"]
    if search:
        cmd.append("--search")
    cmd.extend(["exec", "--ephemeral", "-s", "read-only"])
    if model:
        cmd.extend(["-m", model])
    cmd.append("-")  # read prompt from stdin

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from exc

    output = stdout.strip()
    if not output:
        stderr_hint = stderr.strip()[:300] if stderr else ""
        raise RuntimeError(
            f"Codex returned empty output (exit {proc.returncode}). "
            f"stderr hint: {stderr_hint}"
        )
    return output


def _extract_json(output: str) -> Optional[dict]:
    """Extract the first ```json ... ``` fenced block from *output*."""
    m = re.search(r'```json\s*\n(.*?)\n\s*```', output, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Phase 1 — Planner
# ---------------------------------------------------------------------------

_PLANNER_PROMPT = """\
You are a research planner. Given a research question and optional context, \
decompose the question into 5-7 independent subquestions that, when answered, \
will provide a comprehensive answer to the main question.

RESEARCH QUESTION:
{question}

CONTEXT:
{context}

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "subquestions": [
    {{
      "id": 1,
      "text": "The subquestion",
      "rationale": "Why this matters for the main question",
      "search_terms": ["term1", "term2"],
      "stop_condition": "What would count as a sufficient answer"
    }}
  ],
  "approach_notes": "Brief strategy notes for answering the main question"
}}
"""


def _run_planner(
    question: str,
    context: str,
    *,
    timeout: int = 120,
    model: Optional[str] = None,
    search: bool = False,
) -> dict:
    prompt = _PLANNER_PROMPT.format(
        question=question,
        context=context or "(none)",
    )
    output = _run_codex(prompt, timeout=timeout, model=model, search=search)
    parsed = _extract_json(output)
    if parsed is None:
        raise ValueError(f"Planner did not return valid JSON. Raw output:\n{output[:500]}")
    return parsed


# ---------------------------------------------------------------------------
# Phase 2 — Worker (one per subquestion)
# ---------------------------------------------------------------------------

_WORKER_PROMPT = """\
You are a research worker performing web research. Answer the subquestion \
below in the context of the main research question.

IMPORTANT: Do NOT perform more than 3 web searches total. After 3 searches, \
stop searching and synthesize what you have found. Prefer targeted searches \
over broad ones.

MAIN QUESTION:
{main_question}

SUBQUESTION (id={sq_id}):
{sq_text}

SEARCH TERMS TO START WITH: {search_terms}

STOP CONDITION: {stop_condition}

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "subquestion_id": {sq_id},
  "answer": "Direct answer to the subquestion (2-4 sentences)",
  "detailed_findings": "Comprehensive findings (multiple paragraphs)",
  "key_evidence": [
    {{
      "claim": "A specific finding",
      "source": "Where this was found",
      "confidence": "high|medium|low"
    }}
  ],
  "competing_views": "Any disagreements or alternative perspectives found",
  "uncertainties": "What remains unclear or unresolved",
  "sources": ["url1", "url2"]
}}
"""


def _run_worker(
    subquestion: dict,
    main_question: str,
    *,
    timeout: int = 300,
    model: Optional[str] = None,
    search: bool = True,
) -> dict:
    prompt = _WORKER_PROMPT.format(
        main_question=main_question,
        sq_id=subquestion["id"],
        sq_text=subquestion["text"],
        search_terms=", ".join(subquestion.get("search_terms", [])),
        stop_condition=subquestion.get("stop_condition", "N/A"),
    )
    output = _run_codex(prompt, timeout=timeout, model=model, search=search)
    parsed = _extract_json(output)
    if parsed is None:
        raise ValueError(
            f"Worker (sq {subquestion['id']}) did not return valid JSON. "
            f"Raw output:\n{output[:500]}"
        )
    parsed["subquestion_id"] = subquestion["id"]
    return parsed


# ---------------------------------------------------------------------------
# Phase 3 — Aggregator
# ---------------------------------------------------------------------------

_AGGREGATOR_PROMPT = """\
You are a research aggregator. You have been given subreports from several \
research workers who each investigated a subquestion of the main research \
question. Synthesize their findings into a coherent, comprehensive answer.

MAIN QUESTION:
{question}

RESEARCH PLAN:
{plan}

WORKER SUBREPORTS:
{worker_results}

Instructions:
- Resolve any contradictions between subreports (note which you resolved and how)
- Separate established facts from hypotheses/opinions
- Identify the top 3-5 remaining uncertainties
- Provide a direct, comprehensive answer to the main question
- List recommended next steps for further research

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "direct_answer": "Comprehensive answer to the main question (multiple paragraphs)",
  "key_findings": [
    {{
      "finding": "A key finding",
      "confidence": "high|medium|low",
      "supporting_sources": ["source1", "source2"]
    }}
  ],
  "conflicts_resolved": [
    {{
      "conflict": "Description of the contradiction",
      "resolution": "How it was resolved",
      "favored_view": "Which view the evidence supports"
    }}
  ],
  "facts_vs_hypotheses": {{
    "established_facts": ["fact1", "fact2"],
    "hypotheses": ["hypothesis1", "hypothesis2"]
  }},
  "top_uncertainties": ["uncertainty1", "uncertainty2"],
  "recommended_next_steps": ["step1", "step2"],
  "all_sources": ["url1", "url2"]
}}
"""


def _run_aggregator(
    question: str,
    plan: dict,
    worker_results: List[dict],
    *,
    timeout: int = 180,
    model: Optional[str] = None,
) -> dict:
    prompt = _AGGREGATOR_PROMPT.format(
        question=question,
        plan=json.dumps(plan, indent=2),
        worker_results=json.dumps(worker_results, indent=2),
    )
    # No --search: aggregator works only from gathered evidence
    output = _run_codex(prompt, timeout=timeout, model=model, search=False)
    parsed = _extract_json(output)
    if parsed is None:
        raise ValueError(f"Aggregator did not return valid JSON. Raw output:\n{output[:500]}")
    return parsed


# ---------------------------------------------------------------------------
# Phase 4 — Reviewer
# ---------------------------------------------------------------------------

_REVIEWER_PROMPT = """\
You are a research quality reviewer. Critically evaluate the research synthesis \
below. Check for contradictions, unsupported claims, coverage gaps, and overall \
quality.

MAIN QUESTION:
{question}

RESEARCH PLAN:
{plan}

WORKER SUBREPORTS:
{worker_results}

SYNTHESIS:
{synthesis}

Evaluate and respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "overall_grade": "A|B|C|D",
  "grade_rationale": "Why this grade was given",
  "contradictions_found": [
    {{
      "description": "The contradiction",
      "severity": "high|medium|low",
      "recommendation": "How to address it"
    }}
  ],
  "unsupported_claims": [
    {{
      "claim": "The unsupported claim",
      "what_is_missing": "What evidence would be needed"
    }}
  ],
  "coverage_gaps": ["gap1", "gap2"],
  "strengths": ["strength1", "strength2"],
  "suggestions_for_improvement": ["suggestion1", "suggestion2"]
}}
"""


def _run_reviewer(
    question: str,
    plan: dict,
    worker_results: List[dict],
    synthesis: dict,
    *,
    timeout: int = 180,
    model: Optional[str] = None,
) -> dict:
    prompt = _REVIEWER_PROMPT.format(
        question=question,
        plan=json.dumps(plan, indent=2),
        worker_results=json.dumps(worker_results, indent=2),
        synthesis=json.dumps(synthesis, indent=2),
    )
    # No --search: reviewer works only from existing evidence
    output = _run_codex(prompt, timeout=timeout, model=model, search=False)
    parsed = _extract_json(output)
    if parsed is None:
        raise ValueError(f"Reviewer did not return valid JSON. Raw output:\n{output[:500]}")
    return parsed


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _generate_report_md(
    question: str,
    plan: dict,
    synthesis: dict,
    review: Optional[dict],
) -> str:
    """Generate a human-readable markdown report from research results."""
    lines: list[str] = []

    lines.append(f"# Deep Research Report")
    lines.append(f"")
    lines.append(f"**Question:** {question}")
    lines.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if review:
        lines.append(f"**Quality Grade:** {review.get('overall_grade', 'N/A')}")
    lines.append("")

    # Executive summary
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(synthesis.get("direct_answer", "(No direct answer produced)"))
    lines.append("")

    # Key findings
    findings = synthesis.get("key_findings", [])
    if findings:
        lines.append("## Key Findings")
        lines.append("")
        for f in findings:
            confidence = f.get("confidence", "unknown")
            lines.append(f"- **[{confidence}]** {f.get('finding', '')}")
            sources = f.get("supporting_sources", [])
            if sources:
                lines.append(f"  - Sources: {', '.join(sources)}")
        lines.append("")

    # Conflicts & resolutions
    conflicts = synthesis.get("conflicts_resolved", [])
    if conflicts:
        lines.append("## Conflicts & Resolutions")
        lines.append("")
        for c in conflicts:
            lines.append(f"- **Conflict:** {c.get('conflict', '')}")
            lines.append(f"  - **Resolution:** {c.get('resolution', '')}")
            lines.append(f"  - **Favored view:** {c.get('favored_view', '')}")
        lines.append("")

    # Facts vs hypotheses
    fvh = synthesis.get("facts_vs_hypotheses", {})
    facts = fvh.get("established_facts", [])
    hypotheses = fvh.get("hypotheses", [])
    if facts or hypotheses:
        lines.append("## Facts vs. Hypotheses")
        lines.append("")
        if facts:
            lines.append("### Established Facts")
            for fact in facts:
                lines.append(f"- {fact}")
            lines.append("")
        if hypotheses:
            lines.append("### Hypotheses (not fully established)")
            for h in hypotheses:
                lines.append(f"- {h}")
            lines.append("")

    # Uncertainties
    uncertainties = synthesis.get("top_uncertainties", [])
    if uncertainties:
        lines.append("## Top Uncertainties")
        lines.append("")
        for u in uncertainties:
            lines.append(f"- {u}")
        lines.append("")

    # Recommendations
    next_steps = synthesis.get("recommended_next_steps", [])
    if next_steps:
        lines.append("## Recommended Next Steps")
        lines.append("")
        for s in next_steps:
            lines.append(f"- {s}")
        lines.append("")

    # Quality review
    if review:
        lines.append("## Quality Review")
        lines.append("")
        lines.append(f"**Grade:** {review.get('overall_grade', 'N/A')} — {review.get('grade_rationale', '')}")
        lines.append("")

        strengths = review.get("strengths", [])
        if strengths:
            lines.append("### Strengths")
            for s in strengths:
                lines.append(f"- {s}")
            lines.append("")

        gaps = review.get("coverage_gaps", [])
        if gaps:
            lines.append("### Coverage Gaps")
            for g in gaps:
                lines.append(f"- {g}")
            lines.append("")

        unsupported = review.get("unsupported_claims", [])
        if unsupported:
            lines.append("### Unsupported Claims")
            for u in unsupported:
                lines.append(f"- **Claim:** {u.get('claim', '')}")
                lines.append(f"  - Missing: {u.get('what_is_missing', '')}")
            lines.append("")

        suggestions = review.get("suggestions_for_improvement", [])
        if suggestions:
            lines.append("### Suggestions for Improvement")
            for s in suggestions:
                lines.append(f"- {s}")
            lines.append("")

    # Sources
    all_sources = synthesis.get("all_sources", [])
    if all_sources:
        lines.append("## Sources")
        lines.append("")
        for src in all_sources:
            lines.append(f"- {src}")
        lines.append("")

    # Approach notes
    approach = plan.get("approach_notes", "")
    if approach:
        lines.append("## Research Approach")
        lines.append("")
        lines.append(approach)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_research(
    question: str,
    *,
    context: str = "",
    output_dir: Optional[str] = None,
    max_workers: int = 5,
    max_subquestions: Optional[int] = None,
    model: Optional[str] = None,
    search: bool = True,
    planner_timeout: int = 600,
    worker_timeout: int = 900,
    aggregator_timeout: int = 600,
    reviewer_timeout: int = 600,
) -> dict:
    """Run a full deep research pipeline on *question*.

    Args:
        question: The main research question.
        context: Optional context to guide the planner.
        output_dir: Directory for intermediate + final outputs. Auto-generated
            under ``research_output/YYYY-MM-DD/{slug}/`` if not provided.
        max_workers: Maximum parallel worker threads.
        max_subquestions: Optional cap on planner output to keep the worker
            fan-out bounded for short task budgets.
        model: Codex model override (e.g. ``o4-mini``).
        search: Enable ``--search`` on planner + workers (default True).
        planner_timeout: Planner phase timeout in seconds.
        worker_timeout: Per-worker timeout in seconds.
        aggregator_timeout: Aggregator phase timeout in seconds.
        reviewer_timeout: Reviewer phase timeout in seconds.

    Returns:
        Dict with ``status``, ``report_path``, ``plan``, ``worker_results``,
        ``synthesis``, ``review``, ``errors``, and ``metadata``.
    """
    started_at = datetime.now()
    errors: list[dict] = []

    # Ensure codex is available
    if not shutil.which("codex"):
        return {
            "status": "failed",
            "errors": [{"type": "setup", "message": "codex CLI not found on PATH"}],
            "metadata": {"started_at": started_at.isoformat(), "duration_s": 0},
        }

    # Resolve output directory
    if output_dir is None:
        slug = re.sub(r'[^a-z0-9]+', '-', question.lower()[:60]).strip('-')
        date_str = started_at.strftime("%Y-%m-%d")
        out = Path("research_output") / date_str / slug
    else:
        out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    workers_dir = out / "workers"
    workers_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Phase 1: Planner
    # ------------------------------------------------------------------
    print(f"[deep_research] Phase 1/4: Planning — decomposing question...")
    try:
        plan = _run_planner(
            question, context, timeout=planner_timeout, model=model, search=search,
        )
        subquestions = plan.get("subquestions", [])
        if max_subquestions and len(subquestions) > max_subquestions:
            original_count = len(subquestions)
            subquestions = subquestions[:max_subquestions]
            plan["subquestions"] = subquestions
            plan["truncation"] = {
                "applied": True,
                "original_subquestion_count": original_count,
                "kept_subquestion_count": len(subquestions),
            }
            print(
                "  -> Truncated planner output to "
                f"{len(subquestions)} subquestions (from {original_count})"
            )
        (out / "plan.json").write_text(json.dumps(plan, indent=2))
        print(f"  -> {len(subquestions)} subquestions generated")
    except Exception as e:
        print(f"  -> Planner FAILED: {e}", file=sys.stderr)
        return {
            "status": "failed",
            "errors": [{"type": "planner", "message": str(e)}],
            "output_dir": str(out),
            "metadata": {
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now().isoformat(),
                "duration_s": (datetime.now() - started_at).total_seconds(),
            },
        }

    if not subquestions:
        return {
            "status": "failed",
            "errors": [{"type": "planner", "message": "Planner returned zero subquestions"}],
            "output_dir": str(out),
            "metadata": {
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now().isoformat(),
                "duration_s": (datetime.now() - started_at).total_seconds(),
            },
        }

    # ------------------------------------------------------------------
    # Phase 2: Workers (parallel)
    # ------------------------------------------------------------------
    print(f"[deep_research] Phase 2/4: Workers — researching {len(subquestions)} subquestions (max_workers={max_workers})...")
    worker_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _run_worker,
                sq,
                question,
                timeout=worker_timeout,
                model=model,
                search=search,
            ): sq
            for sq in subquestions
        }
        for future in as_completed(futures):
            sq = futures[future]
            sq_id = sq.get("id", "?")
            try:
                wr = future.result()
                worker_results.append(wr)
                (workers_dir / f"worker_{sq_id}.json").write_text(
                    json.dumps(wr, indent=2)
                )
                print(f"  -> Worker {sq_id} done")
            except Exception as e:
                print(f"  -> Worker {sq_id} FAILED: {e}", file=sys.stderr)
                errors.append({"type": f"worker_{sq_id}", "message": str(e)})

    successful_workers = len(worker_results)
    total_workers = len(subquestions)
    print(f"  -> {successful_workers}/{total_workers} workers succeeded")

    if successful_workers < 2:
        return {
            "status": "failed",
            "plan": plan,
            "worker_results": worker_results,
            "errors": errors + [{"type": "workers", "message": f"Only {successful_workers} workers succeeded (need >=2)"}],
            "output_dir": str(out),
            "metadata": {
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now().isoformat(),
                "duration_s": (datetime.now() - started_at).total_seconds(),
            },
        }

    # ------------------------------------------------------------------
    # Phase 3: Aggregator
    # ------------------------------------------------------------------
    print("[deep_research] Phase 3/4: Aggregating worker findings...")
    synthesis = None
    try:
        synthesis = _run_aggregator(
            question, plan, worker_results,
            timeout=aggregator_timeout, model=model,
        )
        (out / "aggregated.json").write_text(json.dumps(synthesis, indent=2))
        print("  -> Aggregation complete")
    except Exception as e:
        print(f"  -> Aggregator FAILED: {e}", file=sys.stderr)
        errors.append({"type": "aggregator", "message": str(e)})

    # If aggregator failed, build a minimal synthesis from raw workers
    if synthesis is None:
        synthesis = {
            "direct_answer": "Aggregation failed. Below are raw worker findings.",
            "key_findings": [
                {"finding": wr.get("answer", ""), "confidence": "low", "supporting_sources": wr.get("sources", [])}
                for wr in worker_results
            ],
            "conflicts_resolved": [],
            "facts_vs_hypotheses": {"established_facts": [], "hypotheses": []},
            "top_uncertainties": ["Aggregation failed — findings not cross-checked"],
            "recommended_next_steps": ["Re-run with working aggregator"],
            "all_sources": [s for wr in worker_results for s in wr.get("sources", [])],
        }
        (out / "aggregated.json").write_text(json.dumps(synthesis, indent=2))

    # ------------------------------------------------------------------
    # Phase 4: Reviewer
    # ------------------------------------------------------------------
    print("[deep_research] Phase 4/4: Quality review...")
    review = None
    try:
        review = _run_reviewer(
            question, plan, worker_results, synthesis,
            timeout=reviewer_timeout, model=model,
        )
        (out / "review.json").write_text(json.dumps(review, indent=2))
        print(f"  -> Review complete — grade: {review.get('overall_grade', '?')}")
    except Exception as e:
        print(f"  -> Reviewer FAILED: {e}", file=sys.stderr)
        errors.append({"type": "reviewer", "message": str(e)})

    # ------------------------------------------------------------------
    # Generate report
    # ------------------------------------------------------------------
    report_md = _generate_report_md(question, plan, synthesis, review)
    report_path = out / "report.md"
    report_path.write_text(report_md, encoding="utf-8")

    # Determine overall status
    aggregator_ok = "aggregator" not in {e["type"] for e in errors}
    reviewer_ok = "reviewer" not in {e["type"] for e in errors}
    if aggregator_ok and reviewer_ok and successful_workers == total_workers:
        status = "success"
    else:
        status = "partial"

    ended_at = datetime.now()
    result = {
        "status": status,
        "question": question,
        "output_dir": str(out),
        "report_path": str(report_path),
        "plan": plan,
        "worker_results": worker_results,
        "synthesis": synthesis,
        "review": review,
        "errors": errors,
        "metadata": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_s": round((ended_at - started_at).total_seconds(), 1),
            "workers_succeeded": successful_workers,
            "workers_total": total_workers,
        },
    }

    # Save combined result
    (out / "research_result.json").write_text(json.dumps(result, indent=2))

    print(f"[deep_research] Done — status={status}, report at {report_path}")
    return result
