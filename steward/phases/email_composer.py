"""Compose multi-project LTT email.

Two composers are available:

  - compose_dashboard(...)  — original deterministic template (default).
  - compose_narrative(...)  — LLM-generated research-voice body that treats
                              each completed experiment as a mini-paper
                              (Method / Baselines / Implementation / Dataset /
                              Metrics / Results / Interpretation labels).

A per-task charter selects between them via `report.style` (see
`steward.phases.feedback.send_ltt_email`). When `style` is absent or
"dashboard", behavior is identical to the pre-change shipped version.

For narrative mode, each project's `projects_status[i]` must include
`task_dir` (the task's filesystem path under `tasks/`) so the composer can
load per-step artifacts. If `task_dir` is missing, narrative falls back to
dashboard for that project group.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Existing dashboard composer — unchanged
# ---------------------------------------------------------------------------

def compose_dashboard(projects_status: list[dict], global_cycle: int) -> str:
    """Build markdown email body from project statuses (legacy template).

    Args:
        projects_status: List of dicts with project_id, status, definition, cycle_summary.
        global_cycle: The global LTT cycle number.

    Returns:
        Markdown string for email body.
    """
    lines = []
    lines.append(f"# Research Agent Daily Report — Cycle {global_cycle}")
    lines.append("")

    # Dashboard table
    lines.append("## Dashboard")
    lines.append("")
    lines.append("| Project | Confidence | Status | Needs Input? | Days Since Reply |")
    lines.append("|---------|-----------|--------|-------------|-----------------|")

    needs_input = []
    on_track = []

    for ps in projects_status:
        pid = ps["project_id"]
        status = ps["status"]
        conf = status.get("confidence_score", "?")
        needs = status.get("needs_human_input", False)
        days = ps.get("days_since_reply", 0)

        if needs:
            status_label = "Needs input"
        elif conf and int(conf) <= 2:
            status_label = "Low confidence"
        else:
            status_label = "On track"

        needs_label = "**YES**" if needs else "No"
        lines.append(f"| {pid} | {conf}/5 | {status_label} | {needs_label} | {days} |")

        if needs:
            needs_input.append(ps)
        else:
            on_track.append(ps)

    lines.append("")

    if needs_input:
        lines.append("## Projects Needing Input (read these)")
        lines.append("")
        for ps in needs_input:
            lines.append(f"### {ps['project_id']}")
            lines.append("")
            summary = ps.get("cycle_summary", {})
            lines.append(summary.get("tldr", "No summary available."))
            lines.append("")
            questions = ps["status"].get("human_input_questions", [])
            if questions:
                lines.append("**Questions for you:**")
                for q in questions:
                    lines.append(f"- {q}")
                lines.append("")

    if on_track:
        lines.append("## Projects On Track (skim or skip)")
        lines.append("")
        for ps in on_track:
            lines.append(f"### {ps['project_id']}")
            lines.append("")
            summary = ps.get("cycle_summary", {})
            lines.append(summary.get("tldr", "No summary available."))
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Full Status Documents")
    lines.append("")
    for ps in projects_status:
        lines.append(f"### {ps['project_id']} (Cycle {ps['status'].get('cycle_number', '?')})")
        lines.append("")
        status = ps["status"]
        lines.append(f"**Hypothesis:** {status.get('current_hypothesis', 'N/A')}")
        lines.append("")

        findings = status.get("key_findings", [])
        if findings:
            lines.append("**Key Findings:**")
            for f in findings:
                score = f.get("relevance_score", "?")
                lines.append(f"- [{score}/5] {f.get('finding', '')}")
                prov = f.get("provenance", "")
                if prov:
                    lines.append(f"  - Source: {prov}")
            lines.append("")

        questions = status.get("open_questions", [])
        if questions:
            lines.append("**Open Questions:**")
            for q in questions:
                pri = q.get("priority", "medium")
                lines.append(f"- [{pri}] {q.get('question', '')}")
            lines.append("")

        suggestions = status.get("action_suggestions", [])
        if suggestions:
            lines.append("**Suggested Actions:**")
            for s in suggestions:
                novel = " (NEW)" if s.get("novel") else ""
                lines.append(f"- {s.get('action', '')}{novel}")
                if s.get("rationale"):
                    lines.append(f"  - Rationale: {s['rationale']}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Narrative composer (LLM-generated research-voice body)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_text_trunc(path: Path, max_chars: int = 4500) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n# [...truncated, original {len(text)} chars]"
    return text


def _load_log_entry_for_step(state_dir: Path, step_id: str) -> dict:
    """Most recent successful exploration_log entry matching this step_id."""
    log_path = state_dir / "exploration_log.jsonl"
    if not log_path.exists():
        return {}
    match: dict = {}
    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("step_id") == step_id and entry.get("status") == "success":
                match = entry
    return match


def _gather_cycle_research(state_dir: Path, cycle_number: int, max_n: int = 8) -> list[dict]:
    """Pull research-type exploration_log entries for the given cycle.

    Research entries are entries with `type != "experiment"` (steward's
    Phase 2 doesn't set `type` explicitly; only Phase 2b does, with
    `type: "experiment"`). Includes both successful and failed queries so
    timeouts surface in the email.
    """
    log_path = state_dir / "exploration_log.jsonl"
    if not log_path.exists():
        return []
    out: list[dict] = []
    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("cycle") != cycle_number:
                continue
            if entry.get("type") == "experiment":
                continue
            out.append(entry)
    return out[-max_n:]


def _classify_research_status(entry: dict) -> str:
    """Heuristic 'succeeded / partial / failed' from a research log entry."""
    if entry.get("status") in ("failed", "error", "timeout"):
        return "failed"
    conclusion = (entry.get("conclusion") or "").lower()
    if conclusion.startswith("failed:") or "timeout" in conclusion or "timed out" in conclusion:
        return "failed"
    sources = entry.get("sources_found") or []
    if not sources:
        return "partial"
    return "succeeded"


def _step_artifacts(step: dict, state_dir: Path, repo_path: Path) -> dict:
    step_id = step.get("step_id", "")
    code_path = repo_path / "experiments" / f"{step_id}.py"
    results_path = repo_path / "results" / f"{step_id}_results.json"
    cost_path = repo_path / "results" / f"{step_id}_cost.json"
    return {
        "step_id": step_id,
        "description": step.get("description", ""),
        "completed_at": step.get("completed_at", ""),
        "findings_count": step.get("findings_count", 0),
        "code_path_exists": code_path.exists(),
        "code_excerpt": _read_text_trunc(code_path, max_chars=4500),
        "results": _load_json(results_path),
        "cost": _load_json(cost_path),
        "log_entry": {
            "cycle": _load_log_entry_for_step(state_dir, step_id).get("cycle"),
            "query": _load_log_entry_for_step(state_dir, step_id).get("query", ""),
            "conclusion": _load_log_entry_for_step(state_dir, step_id).get("conclusion", ""),
            "sources_found": _load_log_entry_for_step(state_dir, step_id).get("sources_found", []),
        },
    }


def _gather_recent_steps(state_dir: Path, repo_path: Path, max_steps: int = 6) -> list[dict]:
    state = _load_json(state_dir / "experiment_state.json")
    completed = state.get("completed_steps", []) or []
    recent = list(reversed(completed))[:max_steps]
    return [_step_artifacts(step, state_dir, repo_path) for step in recent]


_NARRATIVE_PROMPT = """You are writing a research email update for the principal
researcher on a research project. The reader is a researcher who wants each
research query and each completed experiment treated as a labeled mini-
report, with structure that ADAPTS to whether this was a research-heavy or
experiment-heavy cycle.

STRICT RULES — apply to everything you write:

  WRITE  : per-query and per-experiment mini-reports with the exact labels
           specified for each section below.
  WRITE  : actual numeric/qualitative results from the bundled JSON, not
           prose summaries of conclusion fields.
  WRITE  : paper URLs / GitHub URLs when external code, methods, or sources
           are used.
  DO NOT : write step IDs, internal file paths, commit hashes, or
           `experiment:<step_id>` strings.
  DO NOT : use "Implemented X", "Added support for Y", "Refactored Z".
  DO NOT : start with "Hello"; do not use emojis or marketing voice.
  DO NOT : invent baselines/datasets/papers/sources not present in the bundles.

REQUIRED STRUCTURE — produce EXACTLY these sections, in this order, with
these headers:

# Cycle {cycle_number} — Executive Read

(1–3 sentences. The single most important result this cycle and what it
means for the central claim. State whether the cycle was research-heavy or
experiment-heavy, and what kind of progress was made.)

# Research this cycle

(One mini-report block per research query in the RESEARCH BUNDLES below.
If RESEARCH BUNDLES is empty AND cycle_mode is experiment_heavy, write the
single line: "Skipped (experiment-heavy cycle)" — nothing else. If RESEARCH
BUNDLES is empty AND cycle_mode is research_heavy, write the single line:
"Phase 2 ran but produced no logged queries" — nothing else. Otherwise, for
each query in the bundles, use exactly these labels, in this order:)

    ## <Headline of what the query learned, NOT the literal query>
    **Question.** <The query verbatim, lightly cleaned (strip newlines)>
    **Status.** Succeeded | Partial | Failed (state the reason for partial/failed in one short clause, e.g. "timed out", "no sources returned")
    **Sources consulted.** <Paper / arXiv / GitHub URLs from sources_found, with a brief descriptor per source. If empty, write "(none)".>
    **Key takeaway.** <1–2 sentences synthesizing what the query established. If Failed, write "(no answer — query failed; see Status)".>

# Experiments this cycle

(One mini-paper block per completed experiment in the EXPERIMENT BUNDLES
below, most recent first. If EXPERIMENT BUNDLES is empty AND cycle_mode is
research_heavy, write the single line: "Skipped (research-heavy cycle)" —
nothing else. If EXPERIMENT BUNDLES is empty AND cycle_mode is
experiment_heavy, write the single line: "Phase 2b ran but no step
completed this cycle" — nothing else. Otherwise, for each step use exactly
these labels, in this order:)

    ## <Headline of what was learned, NOT a step_id>
    **Method.** <Technique in 1–2 sentences>
    **Baselines.** <Conditions compared by name>
    **Implementation.** <Hand-coded | wrapped library X | adapted from paper Y (URL) — plus key non-default parameters>
    **Dataset / Inputs.** <Benchmark + subset; or "synthetic pool of N" if hand-crafted. Cite the upstream issue/PR URL if available.>
    **Metrics.** <What was measured>
    **Results.** <Actual numbers / qualitative outcomes — pulled from the results JSON bundles>
    **Interpretation.** <1–2 sentences tying the numbers to the central claim>

# Does this move the locked goals?

(For EACH locked thing-to-show listed below, one line in this format:

  - <goal text> — **supported** | **partial** | **no evidence yet** — <1-sentence evidence basis>)

# Hypothesis update

(The current hypothesis as one paragraph. If direction_changed=True open
with "Direction shift this cycle:" otherwise open with "Hypothesis stable:".
Note: a confidence jump on research-only evidence is fine to flag but should
not be inflated to "supported" status in section 3.)

# Open questions still unanswered

(Top 3–5 in research voice — "we don't yet know whether…".)

# Suggested next cycle

(Top 2–3 action suggestions reframed as questions to test, each one line:

  - <Question to test> — <one-sentence rationale tying it to the central claim>

Drop any suggestion that reads like an engineering task.)

---
INPUTS YOU MUST USE:

Cycle number: {cycle_number}
Cycle mode (this cycle's rotation): {cycle_mode}
Confidence: {confidence}/5
Direction changed from prior cycle: {direction_changed}

Project goal (locked, for context — do not quote in full):
{goal}

Locked things-to-show (each must appear in the locked-goals section):
{things_to_show_block}

Current hypothesis:
{hypothesis}

Prior cycle hypothesis (for direction-change framing):
{prior_hypothesis}

Open questions:
{questions_block}

Action suggestions (filter ruthlessly; drop engineering-shaped items):
{suggestions_block}

RESEARCH BUNDLES — Phase-2 queries from this cycle. For each you will see:
  - query (the literal research question fired)
  - status (succeeded / partial / failed; classify by sources + conclusion)
  - sources_found (URLs and brief metadata)
  - conclusion (if logged)

{research_bundles_block}

EXPERIMENT BUNDLES — completed Phase-2b steps. For each you will see:
  - description (LLM-written intent from when the experiment was planned)
  - log conclusion + sources_found (post-run summary)
  - results JSON (THE actual numbers — use these for the Results label)
  - cost JSON (optional)
  - code excerpt (read for Method / Implementation / Dataset hints — but
    NEVER quote raw file paths or imports as the email content. Translate
    them into plain method/dataset language.)

{bundles_block}

---

Produce ONLY the markdown email body. No preamble, no fenced code block, no
JSON. Begin with "# Cycle {cycle_number} — Executive Read" on the first line.
"""


def _format_questions(questions: list[dict]) -> str:
    if not questions:
        return "(none)"
    lines = []
    for q in questions[:8]:
        pri = q.get("priority", "medium")
        qtext = (q.get("question", "") or "").strip()
        lines.append(f"- [{pri}] {qtext}")
    return "\n".join(lines)


def _format_suggestions(suggestions: list[dict]) -> str:
    if not suggestions:
        return "(none)"
    lines = []
    for s in suggestions[:8]:
        atext = (s.get("action", "") or "").strip()
        novel = " (novel)" if s.get("novel") else ""
        stype = s.get("type", "")
        rationale = (s.get("rationale", "") or "").strip()
        lines.append(f"- [{stype}{novel}] {atext}\n    rationale: {rationale}")
    return "\n".join(lines)


def _format_things(things: list[str]) -> str:
    if not things:
        return "(none)"
    return "\n".join(f"- {t}" for t in things)


def _format_research_bundles(entries: list[dict]) -> str:
    if not entries:
        return "(no research entries logged for this cycle)"
    parts = []
    for i, e in enumerate(entries, start=1):
        query = (e.get("query") or "").strip().replace("\n", " ")
        status = _classify_research_status(e)
        conclusion = (e.get("conclusion") or "").strip().replace("\n", " ")
        sources = e.get("sources_found") or []
        src_lines = []
        for s in sources[:8]:
            url = s.get("url_or_doi") or s.get("url") or s.get("doi") or ""
            title = (s.get("title") or "").strip()
            summary = (s.get("summary") or "").strip()
            desc = " — ".join(p for p in [title, summary] if p)
            src_lines.append(f"      - {url} {('— ' + desc) if desc else ''}".rstrip())
        src_block = "\n".join(src_lines) if src_lines else "      (no sources_found)"
        parts.append(
            f"---- RESEARCH BUNDLE {i} ----\n"
            f"query: {query}\n"
            f"classified_status: {status}\n"
            f"conclusion: {conclusion or '(none)'}\n"
            f"sources_found:\n{src_block}"
        )
    return "\n\n".join(parts)


def _format_bundles(bundles: list[dict]) -> str:
    if not bundles:
        return "(no completed experiment steps found)"
    parts = []
    for i, b in enumerate(bundles, start=1):
        results_str = json.dumps(b["results"], indent=2)[:3000] if b["results"] else "(no results JSON)"
        cost_str = json.dumps(b["cost"]) if b["cost"] else "(no cost JSON)"
        sources = b["log_entry"].get("sources_found") or []
        sources_str = "\n".join(
            f"      - {s.get('summary', '')[:200]}"
            for s in sources[:6]
        ) or "      (none)"
        code = b["code_excerpt"] or "(no code file at experiments/<step_id>.py)"
        parts.append(
            f"---- BUNDLE {i} ----\n"
            f"step_id (do NOT quote in email body): {b['step_id']}\n"
            f"planning_intent: {b['description']}\n"
            f"completed_at: {b['completed_at']}\n"
            f"log_conclusion: {b['log_entry'].get('conclusion', '')}\n"
            f"log_sources_found:\n{sources_str}\n"
            f"results_json:\n{results_str}\n"
            f"cost_json: {cost_str}\n"
            f"code_excerpt (read for method/implementation hints — do NOT quote paths):\n"
            f"```python\n{code}\n```"
        )
    return "\n\n".join(parts)


def _direction_changed(prior_hypothesis: str, new_hypothesis: str, threshold: float = 0.85) -> bool:
    """Same similarity rule used by the milestone-push logic in run.py clones."""
    from difflib import SequenceMatcher
    a = (prior_hypothesis or "").strip().lower()
    b = (new_hypothesis or "").strip().lower()
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() < threshold


def compose_narrative_for_project(
    ps: dict,
    cycle_number: int,
    narrative_spec: Optional[dict] = None,
) -> str:
    """Compose narrative body for a single project entry.

    Requires `ps["task_dir"]` to load per-step artifacts. If missing, returns
    the dashboard composition for this single project as a graceful fallback.
    """
    narrative_spec = narrative_spec or {}
    task_dir_str = ps.get("task_dir")
    if not task_dir_str:
        return compose_dashboard([ps], cycle_number)

    task_dir = Path(task_dir_str)
    state_dir = task_dir / "state"
    definition = ps.get("definition") or {}
    repo_path_str = (
        narrative_spec.get("artifacts", {}).get("experiment_repo")
        or definition.get("experiment_repo")
    )
    if not repo_path_str:
        # No experiment artifacts available — fall back to a "no experiments"
        # narrative without per-step bundles.
        bundles = []
    else:
        bundles = _gather_recent_steps(
            state_dir,
            Path(repo_path_str),
            max_steps=int(narrative_spec.get("max_experiments", 6)),
        )

    # Research bundles for THIS cycle (Phase 2 queries — succeeded and failed)
    research_entries = _gather_cycle_research(
        state_dir,
        int(cycle_number) if isinstance(cycle_number, (int, float, str)) else 0,
        max_n=int(narrative_spec.get("max_research_queries", 8)),
    )

    status = ps.get("status") or {}
    confidence = status.get("confidence_score", "?")
    current_hyp = (status.get("current_hypothesis") or "").strip()
    prior_hyp = ""
    prior_path = state_dir / "last_hypothesis.txt"
    if prior_path.exists():
        prior_hyp = prior_path.read_text(errors="replace").strip()
    direction_changed = _direction_changed(prior_hyp, current_hyp)

    # Cycle mode — for the prompt's research/experiment section dispatch.
    # We prefer ps["cycle_summary"]["cycle_mode"] if the caller provided it;
    # else read task_state.json::cycle_mode; else default "unknown".
    cycle_mode = (
        (ps.get("cycle_summary") or {}).get("cycle_mode")
        or _load_json(state_dir / "task_state.json").get("cycle_mode")
        or "unknown"
    )

    prompt = _NARRATIVE_PROMPT.format(
        cycle_number=cycle_number,
        cycle_mode=cycle_mode,
        confidence=confidence,
        direction_changed=str(direction_changed),
        goal=(definition.get("goal") or "")[:1200],
        things_to_show_block=_format_things(definition.get("things_to_show") or []),
        hypothesis=current_hyp[:1500] or "(none)",
        prior_hypothesis=prior_hyp[:1500] or "(no prior hypothesis recorded)",
        questions_block=_format_questions(status.get("open_questions") or []),
        suggestions_block=_format_suggestions(status.get("action_suggestions") or []),
        research_bundles_block=_format_research_bundles(research_entries),
        bundles_block=_format_bundles(bundles),
    )

    # Late import so dashboard-only call sites don't pay the cost.
    from steward.llm import call_llm

    llm_timeout = int(narrative_spec.get("llm_timeout_seconds") or 300)
    body = call_llm(prompt, timeout=llm_timeout) or ""
    body = body.strip()
    if not body.lower().startswith("# cycle"):
        idx = body.find("# Cycle")
        if idx >= 0:
            body = body[idx:]

    footer = (
        "\n\n---\n"
        f"_Cycle {cycle_number} · mode={cycle_mode} · "
        f"confidence {confidence}/5 · "
        f"direction_changed={direction_changed} · "
        f"research_queries_in_email={len(research_entries)} · "
        f"experiments_in_email={len(bundles)} · "
        f"sent {datetime.now().isoformat(timespec='seconds')}_"
    )
    return body + footer


def compose_narrative(
    projects_status: list[dict],
    global_cycle: int,
    narrative_spec: Optional[dict] = None,
) -> str:
    """Top-level narrative composer.

    Single-project case is the supported path; multi-project narrative emits
    sequential per-project narrative blocks separated by a divider.
    """
    if not projects_status:
        return compose_dashboard(projects_status, global_cycle)

    if len(projects_status) == 1:
        return compose_narrative_for_project(
            projects_status[0], global_cycle, narrative_spec
        )

    parts = []
    for ps in projects_status:
        parts.append(f"# Project: {ps.get('project_id', '?')}")
        parts.append("")
        parts.append(compose_narrative_for_project(ps, global_cycle, narrative_spec))
        parts.append("\n---\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def compose_email(
    projects_status: list[dict],
    global_cycle: int,
    style: str = "dashboard",
    narrative_spec: Optional[dict] = None,
) -> str:
    """Pick the composer based on `style`.

    style values:
      - "dashboard" (default) — original deterministic dashboard template
      - "narrative"           — LLM-generated mini-paper-per-experiment body
    """
    if style == "narrative":
        try:
            return compose_narrative(projects_status, global_cycle, narrative_spec)
        except Exception as e:
            print(
                f"  [email_composer] narrative composition failed ({e}); "
                "falling back to dashboard",
                file=sys.stderr,
            )
            return compose_dashboard(projects_status, global_cycle)
    return compose_dashboard(projects_status, global_cycle)
