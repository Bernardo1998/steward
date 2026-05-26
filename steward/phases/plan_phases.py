"""Plan-runner mode phase functions.

`plan_runner` is a sibling worker type to LTT. Where LTT explores divergently
and auto-evolves a hypothesis each cycle, `plan_runner` reads a user-authored
`plan.yaml`, executes one step per cycle, and between user-blessed iterations
only deepens completed steps (ablate / robust / citation_harden /
failure_probe / gap_check). State updates are drafted into
`state/project_state.draft.md` and only become authoritative after the user
approves — either by editing `project_state.md` to match the draft, or by
replying to the cycle email with the approval marker `/approve v<N>`.

The functions in this module form a stable API consumed by
`steward.plan_runner.PlanRunner`. End-users do not call them directly —
they construct a `PlanRunner(task_dir=..., experiment_repo=...,
email_prefix=...)` and call `.run()` once per cycle.

Approval marker (final choice): the runner accepts the body-line regex
`^\\s*/approve\\s+v(\\d+)\\s*$` (case-insensitive) anywhere in the inbound
email. The wider `^\\s*(approve|approved)\\s+v(\\d+)\\s*$` form is NOT
accepted because it collides with prose like "I approve v2 only if ...".

Deepening storage (final choice): runner-proposed deepening actions are
appended to `plan.yaml::deepening_added_by_runner` so the user sees every
proposal in a single file under git tracking. We do NOT use a sidecar
`state/deepening_queue.yaml`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STEP_TYPES = {
    "experiment",
    "research",
    "replicate",
    "ablate",
    "robust",
    "citation_harden",
    "failure_probe",
    "gap_check",
}

VALID_STATUS = {"pending", "in_progress", "completed", "failed", "deepening", "done_enough"}
VALID_READINESS = {"not_yet", "draft", "ready"}

APPROVAL_MARKER_RE = re.compile(
    r"^\s*/approve\s+(?:v(\d+)|([\w-]+))\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_HARD_FILE_EXISTS_RE = re.compile(r"^file_exists:\s*(.+)$")
_HARD_METRIC_RE = re.compile(
    r"^metric:\s*([^:]+)::([\w\.\[\]\-]+)\s+(==|!=|>=|<=|>|<|is\s+number|is\s+string)(?:\s+(.+))?$"
)


# ---------------------------------------------------------------------------
# Atomic helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# §4.1 plan.yaml — load + validate
# ---------------------------------------------------------------------------

def _validate_hard_criterion(criterion: str, step_id: str, idx: int) -> None:
    """Raise ValueError if `criterion` doesn't match the file_exists / metric grammar."""
    if not isinstance(criterion, str):
        raise ValueError(
            f"plan.yaml step '{step_id}' hard[{idx}]: expected string, got {type(criterion).__name__}"
        )
    text = criterion.strip()
    if _HARD_FILE_EXISTS_RE.match(text):
        return
    if _HARD_METRIC_RE.match(text):
        return
    raise ValueError(
        f"plan.yaml step '{step_id}' hard[{idx}]: invalid grammar "
        f"(expected 'file_exists: <path>' or 'metric: <path>::<key> <op> <value>'): {text!r}"
    )


def _validate_step(step: dict, step_ids_so_far: set, source: str) -> None:
    if not isinstance(step, dict):
        raise ValueError(f"{source}: each step must be a dict, got {type(step).__name__}")
    for key in ("id", "type", "description", "success_criteria"):
        if key not in step:
            raise ValueError(f"{source}: step missing required key '{key}': {step!r}")
    sid = step["id"]
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError(f"{source}: step id must be a non-empty string, got {sid!r}")
    if sid in step_ids_so_far:
        raise ValueError(f"{source}: duplicate step id {sid!r}")
    step_ids_so_far.add(sid)

    if step["type"] not in VALID_STEP_TYPES:
        raise ValueError(
            f"{source}: step '{sid}' has invalid type {step['type']!r}; "
            f"must be one of {sorted(VALID_STEP_TYPES)}"
        )

    sc = step["success_criteria"]
    if not isinstance(sc, dict):
        raise ValueError(f"{source}: step '{sid}' success_criteria must be a dict")
    hard = sc.get("hard") or []
    soft = sc.get("soft") or {}
    if not hard and not soft:
        raise ValueError(
            f"{source}: step '{sid}' must have at least one of success_criteria.hard or .soft"
        )
    if hard and not isinstance(hard, list):
        raise ValueError(f"{source}: step '{sid}' success_criteria.hard must be a list")
    for idx, criterion in enumerate(hard or []):
        _validate_hard_criterion(criterion, sid, idx)
    if soft and not isinstance(soft, dict):
        raise ValueError(f"{source}: step '{sid}' success_criteria.soft must be a dict")
    if soft and "rubric" not in soft:
        raise ValueError(f"{source}: step '{sid}' success_criteria.soft must include 'rubric'")


def load_plan(task_dir: Path) -> dict:
    """Read plan.yaml from `task_dir`, validate, and return the parsed dict.

    Raises:
        FileNotFoundError: plan.yaml is missing.
        ValueError: schema violation (missing required keys, invalid types,
            invalid step type enum value, duplicate step id, dangling
            depends_on, malformed hard-criterion grammar).
    """
    plan_path = task_dir / "plan.yaml"
    if not plan_path.exists():
        raise FileNotFoundError(f"plan.yaml not found at {plan_path}")
    raw = _load_yaml(plan_path)
    if not isinstance(raw, dict):
        raise ValueError(f"plan.yaml at {plan_path} must be a mapping at the top level")

    for key in ("version", "title", "approved_at", "approved_by", "parent_state_version"):
        if key not in raw:
            raise ValueError(f"plan.yaml missing required top-level key '{key}'")

    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("plan.yaml::steps must be a non-empty list")

    seen_ids: set[str] = set()
    for step in steps:
        _validate_step(step, seen_ids, "plan.yaml::steps")

    # depends_on validation — must reference earlier steps
    for step in steps:
        deps = step.get("depends_on") or []
        if deps and not isinstance(deps, list):
            raise ValueError(
                f"plan.yaml step '{step['id']}': depends_on must be a list"
            )
        for dep in deps:
            if dep not in seen_ids:
                raise ValueError(
                    f"plan.yaml step '{step['id']}': depends_on references "
                    f"unknown step '{dep}'"
                )

    deepening = raw.get("deepening_added_by_runner") or []
    if deepening:
        if not isinstance(deepening, list):
            raise ValueError("plan.yaml::deepening_added_by_runner must be a list")
        deep_seen: set[str] = set(seen_ids)
        for action in deepening:
            _validate_step(action, deep_seen, "plan.yaml::deepening_added_by_runner")
            if "parent_step_id" not in action:
                raise ValueError(
                    f"plan.yaml deepening action '{action['id']}': "
                    "missing required key 'parent_step_id'"
                )
            if action["parent_step_id"] not in seen_ids:
                raise ValueError(
                    f"plan.yaml deepening action '{action['id']}': "
                    f"parent_step_id {action['parent_step_id']!r} not in plan.steps"
                )

    _validate_stages(raw, seen_ids)

    return raw


def _validate_stages(plan: dict, step_ids: set[str]) -> None:
    """Validate `plan.stages` per §3.5. No-op when stages is absent or empty.

    Rules:
      - Each stage entry needs `id` (str) + `steps` (non-empty list of step ids).
      - Stage ids unique across the plan.
      - Every step id in `stages[].steps[]` must exist in plan.steps.
      - Every step id in plan.steps must appear in exactly one stage.
      - Stage order in the YAML defines advance order.
    """
    stages = plan.get("stages") or []
    if not stages:
        return
    if not isinstance(stages, list):
        raise ValueError("plan.yaml::stages must be a list")

    seen_stage_ids: set[str] = set()
    seen_step_in_stage: set[str] = set()
    for idx, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"plan.yaml::stages[{idx}]: must be a mapping")
        sid = stage.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError(
                f"plan.yaml::stages[{idx}]: missing or invalid 'id' (got {sid!r})"
            )
        if sid in seen_stage_ids:
            raise ValueError(f"plan.yaml::stages: duplicate stage id {sid!r}")
        seen_stage_ids.add(sid)

        steps = stage.get("steps") or []
        if not isinstance(steps, list) or not steps:
            raise ValueError(
                f"plan.yaml::stages[{sid}]: 'steps' must be a non-empty list"
            )
        for step_id in steps:
            if step_id not in step_ids:
                raise ValueError(
                    f"plan.yaml::stages[{sid}]: references unknown step {step_id!r}"
                )
            if step_id in seen_step_in_stage:
                raise ValueError(
                    f"plan.yaml::stages: step {step_id!r} appears in multiple stages"
                )
            seen_step_in_stage.add(step_id)

    missing = step_ids - seen_step_in_stage
    if missing:
        raise ValueError(
            f"plan.yaml::stages: the following plan.steps are not assigned to "
            f"any stage: {sorted(missing)}"
        )


def save_plan(task_dir: Path, plan: dict) -> None:
    """Atomically rewrite plan.yaml. Used when the runner appends a deepening
    action; never used to modify user-authored fields."""
    body = yaml.dump(plan, sort_keys=False, allow_unicode=True, default_flow_style=False)
    _atomic_write(task_dir / "plan.yaml", body)


# ---------------------------------------------------------------------------
# Shared parsing helpers (used by goal.md + plan.yaml loaders)
# ---------------------------------------------------------------------------


def _split_h2_sections(body: str) -> dict[str, str]:
    """Return {section_title_lowered: section_body_without_header} for every
    `## ` heading at column 0. Section bodies are everything between this H2
    and the next H2/EOF, stripped of trailing whitespace."""
    out: dict[str, str] = {}
    current_title: Optional[str] = None
    current_lines: list[str] = []

    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current_title is not None:
                out[current_title] = "\n".join(current_lines).strip("\n")
            current_title = m.group(1).strip().lower()
            current_lines = []
        else:
            if current_title is not None:
                current_lines.append(line)
    if current_title is not None:
        out[current_title] = "\n".join(current_lines).strip("\n")
    return out


def _extract_bullets(section_body: str) -> list[str]:
    bullets = []
    for line in section_body.splitlines():
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            bullets.append(m.group(1).strip())
    return bullets


# ---------------------------------------------------------------------------
# §5.2 select_next_action
# ---------------------------------------------------------------------------

def select_next_action(
    plan: dict,
    plan_state: dict,
    deepening_queue: Optional[list[dict]] = None,
) -> dict:
    """Pick the next action this cycle. v3 (stage-aware) priority rules:

    1. Pending intent question → return ``{"kind": "intent_pending", "step_id": ...}``.
    2. Awaiting stage approval → return ``{"kind": "awaiting_stage_approval",
       "stage_id": ...}``.
    3. Active stage = first stage whose ``stage_progress.status != approved``.
       If no active stage → return ``{"kind": "all_done"}``.
    4. Within the active stage, find an ``in_progress`` step → return it.
       Else find the first ``pending`` step with all ``depends_on`` satisfied
       (deps may live in earlier — already-approved — stages).
    5. If the active stage has no eligible step AND every step in it is
       completed/done_enough → mark the stage ``completed``, set
       ``awaiting_stage_approval``, return that.
    6. Else fall back to deepening / stuck / idle as in v2.

    Returns one of:
      {"kind": "plan_step", "step": <step dict>}
      {"kind": "deepening", "action": <action dict>}
      {"kind": "intent_pending", "step_id": <str>}
      {"kind": "awaiting_stage_approval", "stage_id": <str>}
      {"kind": "all_done"}
      {"kind": "stuck", "reason": <str>}
    """
    if deepening_queue is None:
        deepening_queue = list(plan.get("deepening_added_by_runner") or [])

    step_progress = plan_state.get("step_progress") or {}
    steps = plan.get("steps") or []
    stages = plan.get("stages") or [
        {"id": "all", "steps": [s["id"] for s in steps]}
    ]
    stage_progress = plan_state.get("stage_progress") or {}

    # 1. Intent gate (mid-step design question — caller re-dispatches with answer).
    pending = plan_state.get("pending_intent_question")
    if isinstance(pending, dict) and pending.get("step_id"):
        return {"kind": "intent_pending", "step_id": pending["step_id"]}

    # 2. Stage approval gate.
    awaiting = plan_state.get("awaiting_stage_approval")
    if isinstance(awaiting, dict) and awaiting.get("completed_stage_id"):
        return {
            "kind": "awaiting_stage_approval",
            "stage_id": awaiting["completed_stage_id"],
        }

    # 3. Find active stage.
    active = None
    for st in stages:
        if stage_progress.get(st["id"], {}).get("status") != "approved":
            active = st
            break
    if active is None:
        return {"kind": "all_done"}

    step_by_id = {s["id"]: s for s in steps}
    completed_states = ("completed", "done_enough")
    completed_ids = {
        sid for sid, p in step_progress.items() if p.get("status") in completed_states
    }

    # 4a. Resume any in_progress step within the active stage (in stage order).
    for sid in active["steps"]:
        step = step_by_id.get(sid)
        if not step:
            continue
        if step_progress.get(sid, {}).get("status") == "in_progress":
            return {"kind": "plan_step", "step": step}

    # 4b. First pending step in stage order with deps satisfied.
    for sid in active["steps"]:
        step = step_by_id.get(sid)
        if not step:
            continue
        cur = step_progress.get(sid, {}).get("status", "pending")
        if cur != "pending":
            continue
        deps = step.get("depends_on") or []
        if all(d in completed_ids for d in deps):
            return {"kind": "plan_step", "step": step}

    # 5. If every step in the active stage is completed → mark the stage
    # completed and fire the approval gate.
    all_stage_steps_done = all(
        step_progress.get(sid, {}).get("status") in completed_states + ("failed",)
        for sid in active["steps"]
    )
    if all_stage_steps_done:
        return {"kind": "awaiting_stage_approval", "stage_id": active["id"]}

    # 6a. Within the active stage, eligible deepening?
    stage_step_ids = set(active["steps"])
    eligible_deep = []
    for act in deepening_queue:
        if act.get("parent_step_id") not in stage_step_ids:
            continue
        action_status = step_progress.get(act["id"], {}).get("status", "pending")
        if action_status not in ("pending", "deepening"):
            continue
        parent_id = act.get("parent_step_id")
        parent_prog = step_progress.get(parent_id, {})
        parent_cap = next(
            (s.get("max_deepening_cycles", 4) for s in steps if s["id"] == parent_id),
            4,
        )
        if parent_prog.get("deepening_cycles_done", 0) < parent_cap:
            eligible_deep.append(act)
    if eligible_deep:
        eligible_deep.sort(key=lambda a: a.get("auto_generated_at", ""), reverse=True)
        return {"kind": "deepening", "action": eligible_deep[0]}

    # 6b. stuck counter
    stuck = plan_state.get("stuck_counter", 0)
    max_stuck = plan_state.get("max_stuck_cycles", 5)
    if stuck >= max_stuck:
        return {
            "kind": "stuck",
            "reason": f"stuck_counter={stuck} >= max_stuck_cycles={max_stuck}",
        }

    # 6c. Idle (no pending step, no deepening, not all done) — emit stuck so
    # the cycle email reflects the situation.
    return {
        "kind": "stuck",
        "reason": (
            f"no eligible action in stage '{active['id']}': "
            "neither pending steps with satisfied deps nor in-stage deepening"
        ),
    }


# ---------------------------------------------------------------------------
# §5.3 completion judgment
# ---------------------------------------------------------------------------

def _resolve_json_dotted(obj: Any, dotted_key: str) -> Any:
    """Walk a dotted key (e.g. "outer.inner.field" or "list[0].value") into obj.
    Returns the value or raises KeyError/IndexError/TypeError on miss."""
    cursor: Any = obj
    parts = re.split(r"\.", dotted_key)
    for part in parts:
        # support "key[3]" pattern
        m = re.match(r"^([^\[\]]+)?((?:\[\d+\])*)$", part)
        if not m:
            raise KeyError(f"unparseable dotted key fragment {part!r}")
        head = m.group(1)
        indices = re.findall(r"\[(\d+)\]", m.group(2) or "")
        if head:
            if not isinstance(cursor, dict):
                raise TypeError(f"cannot index {head!r} into {type(cursor).__name__}")
            cursor = cursor[head]
        for idx_str in indices:
            cursor = cursor[int(idx_str)]
    return cursor


_BOOL_TRUE = {"true", "yes"}
_BOOL_FALSE = {"false", "no"}


def _coerce_rhs(token: str) -> Any:
    """Coerce a hard-criterion RHS string to a Python value: bool, None,
    quoted string, int/float if numeric, else the raw string."""
    t = token.strip()
    if not t:
        return t
    lowered = t.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    if lowered in ("null", "none"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _compare(left: Any, op: str, right_repr: str) -> tuple[bool, str]:
    """Apply `op` to `left` and `right_repr` (the raw RHS token).

    Returns (passed, detail). Numeric ops coerce both sides through float
    when possible; equality ops coerce RHS via `_coerce_rhs` so YAML scalars
    like `true`, `false`, `null`, ints, floats, and quoted strings all
    compare correctly to JSON-loaded LHS values.
    """
    if op in ("is number", "is_number"):
        ok = isinstance(left, (int, float)) and not isinstance(left, bool)
        return ok, f"value={left!r}"
    if op in ("is string", "is_string"):
        ok = isinstance(left, str)
        return ok, f"value={left!r}"

    rhs = _coerce_rhs(right_repr)

    if op == "==":
        return left == rhs, f"{left!r} == {rhs!r}"
    if op == "!=":
        return left != rhs, f"{left!r} != {rhs!r}"

    # Numeric-only ops: coerce both sides through float
    try:
        right_num = float(rhs) if rhs is not None else None
    except (TypeError, ValueError):
        right_num = None
    try:
        left_num = float(left)
    except (TypeError, ValueError):
        left_num = None

    if right_num is None or left_num is None:
        return False, (
            f"numeric op {op!r} requires numeric LHS and RHS; "
            f"got left={left!r}, right={right_repr!r}"
        )
    if op == ">":
        return left_num > right_num, f"{left_num} > {right_num}"
    if op == "<":
        return left_num < right_num, f"{left_num} < {right_num}"
    if op == ">=":
        return left_num >= right_num, f"{left_num} >= {right_num}"
    if op == "<=":
        return left_num <= right_num, f"{left_num} <= {right_num}"
    return False, f"unknown op {op!r}"


def judge_hard_criteria(step: dict, experiment_repo: Path) -> dict:
    """Evaluate `step.success_criteria.hard` deterministically.

    Returns:
        {"met": bool, "checks": [{criterion, result, detail}, ...], "missing": [str]}
    """
    hard = (step.get("success_criteria") or {}).get("hard") or []
    checks: list[dict] = []
    missing: list[str] = []
    if not hard:
        return {"met": True, "checks": [], "missing": [], "skipped": "no hard criteria"}

    all_met = True
    for criterion in hard:
        text = criterion.strip()
        m_file = _HARD_FILE_EXISTS_RE.match(text)
        if m_file:
            rel = m_file.group(1).strip()
            target = experiment_repo / rel
            ok = target.exists()
            checks.append({
                "criterion": text,
                "result": ok,
                "detail": f"exists={ok} path={target}",
            })
            if not ok:
                all_met = False
                missing.append(rel)
            continue

        m_metric = _HARD_METRIC_RE.match(text)
        if m_metric:
            rel = m_metric.group(1).strip()
            dotted_key = m_metric.group(2).strip()
            op = m_metric.group(3).strip()
            rhs = (m_metric.group(4) or "").strip()
            target = experiment_repo / rel
            if not target.exists():
                checks.append({
                    "criterion": text,
                    "result": False,
                    "detail": f"file missing: {target}",
                })
                all_met = False
                missing.append(rel)
                continue
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception as e:
                checks.append({
                    "criterion": text,
                    "result": False,
                    "detail": f"json load failed: {e}",
                })
                all_met = False
                continue
            try:
                value = _resolve_json_dotted(data, dotted_key)
            except Exception as e:
                checks.append({
                    "criterion": text,
                    "result": False,
                    "detail": f"key {dotted_key!r} not resolvable: {e}",
                })
                all_met = False
                continue
            passed, detail = _compare(value, op, rhs)
            checks.append({"criterion": text, "result": passed, "detail": detail})
            if not passed:
                all_met = False
            continue

        # Should not happen because load_plan already validated grammar, but be defensive.
        checks.append({
            "criterion": text,
            "result": False,
            "detail": "unrecognized criterion grammar",
        })
        all_met = False

    return {"met": all_met, "checks": checks, "missing": missing}


def judge_soft_criteria(step: dict, bundle: dict, definition: dict) -> dict:
    """LLM rubric judge. Returns {met: bool, rationale: str, confidence: str}.

    `bundle` is the experiment bundle for this step (results JSON + code
    excerpt). `definition` is the project definition.yaml. The LLM is told
    to respond only with JSON.
    """
    soft = (step.get("success_criteria") or {}).get("soft") or {}
    if not soft:
        return {"met": True, "rationale": "no soft rubric", "confidence": "n/a", "skipped": True}

    rubric = soft.get("rubric", "").strip()
    goal_blurb = (definition.get("goal") or "")[:800]
    results_str = json.dumps(bundle.get("results") or {}, indent=2)[:3000]

    prompt = f"""You are evaluating whether an experiment step met its rubric.

Step description: {step.get('description', '')}

Rubric (what the user wants to be true after this step completes):
{rubric}

Step results JSON (the actual artifacts the step produced):
{results_str}

Project locked goal (for context only — do not invent new criteria):
{goal_blurb}

Decide ONLY whether the rubric is met based on the results.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "met": true,
  "rationale": "1-3 sentences. Cite specific numbers or artifacts from the results JSON.",
  "confidence": "low|medium|high"
}}
"""

    # Late import — keep dashboard-only call sites untouched.
    from steward.llm import call_llm_json

    timeout = int(
        (definition.get("experiment_config") or {}).get("soft_criteria_timeout_seconds")
        or definition.get("synthesis_timeout")
        or 120
    )
    try:
        result = call_llm_json(prompt, timeout=timeout)
    except Exception as e:
        return {
            "met": False,
            "rationale": f"LLM call failed: {e}",
            "confidence": "low",
            "error": str(e),
        }
    return {
        "met": bool(result.get("met", False)),
        "rationale": result.get("rationale", ""),
        "confidence": result.get("confidence", "low"),
    }


def judge_step_completion(
    step: dict,
    bundle: dict,
    definition: dict,
    experiment_repo: Path,
) -> dict:
    """Combined judgment per §5.3 decision table.

    Returns {status, rationale, hard_result, soft_result}.
    """
    hard_result = judge_hard_criteria(step, experiment_repo)
    soft_result: Optional[dict] = None

    if hard_result["met"]:
        soft_cfg = (step.get("success_criteria") or {}).get("soft")
        if soft_cfg:
            soft_result = judge_soft_criteria(step, bundle, definition)
            if soft_result.get("met"):
                status = "completed"
                rationale = "Hard criteria met; soft rubric also passed."
            else:
                status = "needs_deepening"
                rationale = (
                    "Hard criteria met but soft rubric failed: "
                    f"{soft_result.get('rationale', '')}"
                )
        else:
            status = "completed"
            rationale = "All hard criteria met; no soft rubric."
    else:
        attempts = (bundle.get("attempts") or 0)
        max_attempts = int(step.get("max_attempts", 3) or 3)
        if attempts < max_attempts:
            status = "in_progress"
            rationale = (
                f"Hard criteria not met (missing={hard_result.get('missing')}); "
                f"attempt {attempts}/{max_attempts} — will retry next cycle."
            )
        else:
            status = "failed"
            rationale = (
                f"Hard criteria not met after {attempts}/{max_attempts} attempts; "
                f"missing={hard_result.get('missing')}."
            )

    return {
        "status": status,
        "rationale": rationale,
        "hard_result": hard_result,
        "soft_result": soft_result,
    }


# ---------------------------------------------------------------------------
# §5.4 deepening action generator
# ---------------------------------------------------------------------------

def generate_deepening_actions(
    completed_step: dict,
    bundle: dict,
    definition: dict,
    n: int = 3,
) -> list[dict]:
    """Propose up to `n` deepening actions strengthening a just-completed step.

    Strict rule communicated to the LLM: deepening actions must NOT advance
    scope. They may only strengthen an EXISTING completed step (ablate,
    robust, citation_harden, failure_probe, gap_check). The LLM is told to
    reject proposals that introduce new modules, datasets, or research
    questions outside the locked things_to_show.
    """
    if n <= 0:
        return []
    things = definition.get("things_to_show") or []
    things_block = "\n".join(f"- {t}" for t in things) or "(none recorded in definition.yaml)"
    results_str = json.dumps(bundle.get("results") or {}, indent=2)[:2500]
    goal_blurb = (definition.get("goal") or "")[:600]

    allowed = sorted(VALID_STEP_TYPES - {"experiment", "research", "replicate"})

    prompt = f"""You are proposing DEEPENING actions for a just-completed plan step.

A deepening action STRENGTHENS the same step. It does NOT advance scope.
You may NOT introduce new modules, new datasets, or new research questions
beyond the locked things_to_show below.

Project goal (locked):
{goal_blurb}

Locked things_to_show (must be the same after deepening):
{things_block}

Just-completed step:
  id: {completed_step.get('id', '')}
  description: {completed_step.get('description', '')}

Step results JSON (cite specific numbers when proposing deepening):
{results_str}

Allowed action types: {allowed}

For each action provide:
- id: kebab-case, prefixed with the parent step id and a suffix that names
  the angle (e.g. "<parent>_ablation_dev_size", "<parent>_robust_seed").
- type: one of {allowed}.
- description: 1-2 sentences. Include enough method/dataset detail that a
  reader knows what would be measured. Do NOT propose new datasets.
- success_criteria.hard: a list of file_exists / metric checks that would
  prove the deepening produced a tangible artifact. At least one entry.
- success_criteria.soft.rubric: one sentence describing the qualitative
  outcome that would satisfy the deepening.
- max_attempts: integer, 1-3, default 2.

Propose up to {n} actions, prioritizing the ones that most strengthen
reviewer-readiness (ablate / robust first; citation_harden / failure_probe /
gap_check after).

If you cannot propose a deepening action without expanding scope, return an
empty list — that is the correct answer.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "actions": [
    {{
      "id": "<parent>_<angle>",
      "type": "ablate",
      "description": "...",
      "success_criteria": {{
        "hard": ["file_exists: results/<id>_results.json"],
        "soft": {{"rubric": "..."}}
      }},
      "max_attempts": 2
    }}
  ]
}}
"""

    from steward.llm import call_llm_json

    timeout = int(
        (definition.get("experiment_config") or {}).get("deepening_timeout_seconds")
        or definition.get("synthesis_timeout")
        or 180
    )
    try:
        result = call_llm_json(prompt, timeout=timeout)
    except Exception as e:
        print(f"  [plan_phases] generate_deepening_actions LLM failed: {e}", file=sys.stderr)
        return []

    proposed = result.get("actions") or []
    out: list[dict] = []
    now_iso = datetime.now().isoformat(timespec="seconds")
    for action in proposed[:n]:
        if not isinstance(action, dict):
            continue
        atype = action.get("type")
        if atype not in VALID_STEP_TYPES:
            print(
                f"  [plan_phases] dropping deepening action with invalid type {atype!r}",
                file=sys.stderr,
            )
            continue
        if atype in ("experiment", "research", "replicate"):
            # Allowed in VALID_STEP_TYPES for user-authored steps, but not for
            # auto-generated deepening (those are scope expansions).
            print(
                f"  [plan_phases] dropping deepening action of type {atype!r} "
                "(only ablate/robust/citation_harden/failure_probe/gap_check allowed)",
                file=sys.stderr,
            )
            continue
        sc = action.get("success_criteria") or {}
        hard = sc.get("hard") or []
        if not isinstance(hard, list) or not hard:
            print(
                f"  [plan_phases] dropping deepening action {action.get('id')!r} "
                "(missing success_criteria.hard)",
                file=sys.stderr,
            )
            continue
        try:
            for idx, c in enumerate(hard):
                _validate_hard_criterion(c, action.get("id", "?"), idx)
        except ValueError as e:
            print(f"  [plan_phases] dropping deepening action: {e}", file=sys.stderr)
            continue
        action.setdefault("description", "")
        action["parent_step_id"] = completed_step.get("id")
        action["auto_generated_at"] = now_iso
        action.setdefault("max_attempts", 2)
        out.append(action)
    return out


def append_deepening_to_plan(task_dir: Path, plan: dict, new_actions: list[dict]) -> dict:
    """Append `new_actions` to plan.yaml::deepening_added_by_runner and rewrite
    the file. Returns the in-memory plan (mutated)."""
    if not new_actions:
        return plan
    existing = plan.get("deepening_added_by_runner") or []
    existing_ids = {a.get("id") for a in existing}
    for action in new_actions:
        if action.get("id") in existing_ids:
            continue
        existing.append(action)
    plan["deepening_added_by_runner"] = existing
    save_plan(task_dir, plan)
    return plan


# ---------------------------------------------------------------------------
# §5.6 reviewer scrutiny (replaces speculate)
# ---------------------------------------------------------------------------

def reviewer_scrutiny(
    plan: dict,
    plan_state: dict,
    completed_bundles: list[dict],
    definition: dict,
) -> dict:
    """Hostile-reviewer LLM call. Output:
      {weaknesses, missing_ablations, would_a_reviewer_accept_this_now, rationale}
    Weaknesses are appended (in-memory) to plan_state.weaknesses_log."""
    if not completed_bundles:
        return {
            "weaknesses": [],
            "missing_ablations": [],
            "would_a_reviewer_accept_this_now": False,
            "rationale": "no completed steps to scrutinize",
        }
    things = definition.get("things_to_show") or []
    things_block = "\n".join(f"- {t}" for t in things) or "(none)"

    bundles_str_parts = []
    for b in completed_bundles[:6]:
        bundles_str_parts.append(
            "---- COMPLETED STEP ----\n"
            f"step_id: {b.get('step_id', '')}\n"
            f"description: {b.get('description', '')}\n"
            f"results_json: {json.dumps(b.get('results') or {}, indent=2)[:1500]}\n"
        )
    bundles_str = "\n\n".join(bundles_str_parts) or "(no bundles)"

    prompt = f"""You are role-playing a hostile peer reviewer of a research project's
RESULTS so far. Your job is to identify what a tough reviewer would cut, what
ablations are missing, and whether a reviewer would accept the current
evidence as published-quality.

Locked things_to_show:
{things_block}

Completed steps + their results:
{bundles_str}

Be specific. Reference numbers from the results. Do NOT propose new scope —
your suggestions must strengthen existing steps, not add new modules or
datasets.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "weaknesses": [
    {{
      "step_id": "<which step this weakness targets>",
      "weakness": "1-2 sentences describing the weakness in reviewer voice",
      "suggested_deepening": "1 sentence describing the deepening action that would address it (type: ablate / robust / citation_harden / failure_probe / gap_check)"
    }}
  ],
  "missing_ablations": ["short text per missing ablation"],
  "would_a_reviewer_accept_this_now": false,
  "rationale": "1-2 sentences summarizing why or why not"
}}
"""

    from steward.llm import call_llm_json

    timeout = int(
        (definition.get("experiment_config") or {}).get("reviewer_scrutiny_timeout_seconds")
        or definition.get("synthesis_timeout")
        or 180
    )
    try:
        result = call_llm_json(prompt, timeout=timeout)
    except Exception as e:
        print(f"  [plan_phases] reviewer_scrutiny LLM failed: {e}", file=sys.stderr)
        return {
            "weaknesses": [],
            "missing_ablations": [],
            "would_a_reviewer_accept_this_now": False,
            "rationale": f"LLM failed: {e}",
        }

    weaknesses = result.get("weaknesses") or []
    # Append to plan_state.weaknesses_log, capped to most recent 20.
    log = list(plan_state.get("weaknesses_log") or [])
    now_iso = datetime.now().isoformat(timespec="seconds")
    for w in weaknesses:
        if isinstance(w, dict):
            w_with_ts = dict(w)
            w_with_ts.setdefault("logged_at", now_iso)
            log.append(w_with_ts)
    plan_state["weaknesses_log"] = log[-20:]
    return {
        "weaknesses": weaknesses,
        "missing_ablations": result.get("missing_ablations") or [],
        "would_a_reviewer_accept_this_now": bool(result.get("would_a_reviewer_accept_this_now")),
        "rationale": result.get("rationale", ""),
    }


# ---------------------------------------------------------------------------
# §5.7 email assist
# ---------------------------------------------------------------------------

def build_email_inputs(
    task_dir: Path,
    plan: dict,
    plan_state: dict,
    cycle_action: dict,
    completed_bundles: list[dict],
    diff: Optional[str] = None,
) -> dict:
    """Pack everything the email composer needs into a single dict, ready to
    be passed via `projects_status[i]['plan_runner_summary']`.

    See §5.7 for the field list.
    """
    step_progress = plan_state.get("step_progress") or {}
    plan_steps = plan.get("steps") or []
    completed_steps_summary = []
    for step in plan_steps:
        sid = step["id"]
        prog = step_progress.get(sid, {})
        completed_steps_summary.append({
            "step_id": sid,
            "headline": (step.get("description", "") or sid).split(".")[0][:140],
            "status": prog.get("status", "pending"),
            "attempts": prog.get("attempts", 0),
            "completed_at": prog.get("completed_at"),
            "last_results_path": prog.get("last_results_path"),
            "deepening_cycles_done": prog.get("deepening_cycles_done", 0),
        })

    deepening_this_cycle = []
    if cycle_action.get("kind") == "deepening":
        deepening_this_cycle.append(cycle_action.get("action") or {})

    # v3.1: stage-level approval gate replaces v2's per-cycle approval gate.
    # `awaiting_approval` (v2) and `awaiting_stage_approval` (v3) are both
    # surfaced in the dict so the composer keeps working through the
    # transition.
    awaiting_stage = plan_state.get("awaiting_stage_approval")
    awaiting = bool(plan_state.get("awaiting_approval") or awaiting_stage)
    diff_preview = None
    if awaiting and diff:
        # Cap to first 60 lines
        diff_preview = "\n".join(diff.splitlines()[:60])

    banner = compute_stage_progress_banner(plan, plan_state, cycle_action)

    return {
        "plan_title": plan.get("title", ""),
        "plan_version": plan.get("version", 1),
        "executed_this_cycle": cycle_action,
        "completed_steps": completed_steps_summary,
        "deepening_this_cycle": deepening_this_cycle,
        "completed_bundles": completed_bundles,
        "awaiting_approval": awaiting,
        "awaiting_stage_approval": awaiting_stage,
        "awaiting_reason": plan_state.get("draft_state_reason"),
        "draft_state_version": plan_state.get("draft_state_version"),
        "draft_diff_preview": diff_preview,
        "reviewer_readiness": plan_state.get("reviewer_readiness") or {},
        "stuck_counter": plan_state.get("stuck_counter", 0),
        "weaknesses_log": (plan_state.get("weaknesses_log") or [])[-6:],
        "task_dir": str(task_dir),
        "pending_intent_question": plan_state.get("pending_intent_question"),
        "stage_progress_banner": banner,
    }


# ---------------------------------------------------------------------------
# §5.5 stage-level approval gate (v3.1)
# ---------------------------------------------------------------------------

def check_stage_approval(
    state_dir: Path,
    state: dict,
    feedback: Optional[dict] = None,
) -> dict:
    """Check whether the user has approved advancing past the
    currently-blocking stage.

    Sources, in order:
      1. ``feedback.body`` / ``feedback.replies[].body`` containing
         ``/approve <stage_id>``, ``/approve next``, or (back-compat)
         ``/approve v<N>``. ANY of these forms count.
      2. ``state.json::approved_through_stage`` hand-edited to match
         ``state.awaiting_stage_approval.completed_stage_id``.

    Returns ``{approved: bool, method: str|None, stage_id: str|None,
    marker: str|None}``. Method is one of: ``email_reply``, ``file_edit``,
    ``None``.
    """
    awaiting = state.get("awaiting_stage_approval") or {}
    blocking_sid = awaiting.get("completed_stage_id")
    if not blocking_sid:
        return {"approved": False, "method": None, "stage_id": None, "marker": None}

    # 1. email reply path
    if feedback:
        candidate_texts: list[str] = []
        if isinstance(feedback, dict):
            if isinstance(feedback.get("body"), str):
                candidate_texts.append(feedback["body"])
            for r in feedback.get("replies") or []:
                if isinstance(r, dict) and isinstance(r.get("body"), str):
                    candidate_texts.append(r["body"])
        for text in candidate_texts:
            for m in APPROVAL_MARKER_RE.finditer(text):
                v_group = m.group(1)
                tok_group = m.group(2)
                marker_text = m.group(0).strip()
                if v_group is not None:
                    # /approve v<N> — back-compat: any v<N> approves the
                    # current blocking stage.
                    return {
                        "approved": True,
                        "method": "email_reply",
                        "stage_id": blocking_sid,
                        "marker": marker_text,
                    }
                token = (tok_group or "").lower()
                if token == "next" or token == blocking_sid.lower():
                    return {
                        "approved": True,
                        "method": "email_reply",
                        "stage_id": blocking_sid,
                        "marker": marker_text,
                    }
                # Any other stage id silently ignored — the user may have
                # written `/approve method` while `setup` is the blocking
                # stage; we don't jump ahead.

    # 2. file_edit path
    approved_through = state.get("approved_through_stage")
    if approved_through and approved_through == blocking_sid:
        return {
            "approved": True,
            "method": "file_edit",
            "stage_id": blocking_sid,
            "marker": None,
        }

    return {"approved": False, "method": None, "stage_id": blocking_sid, "marker": None}


def apply_stage_approval(
    state_dir: Path,
    state: dict,
    approval: dict,
) -> dict:
    """Apply an approval to ``state``. Side effects:
      - ``state.stage_progress[stage_id].status = "approved"`` and
        ``.approved_at = now``.
      - Append entry to ``state.approval_log``.
      - Set ``state.approved_through_stage = stage_id``.
      - Clear ``state.awaiting_stage_approval``.

    Returns the mutated ``state``."""
    sid = approval.get("stage_id")
    method = approval.get("method") or "email_reply"
    marker = approval.get("marker") or ""
    if not sid:
        return state

    now = datetime.now().isoformat(timespec="seconds")
    stage_prog = state.setdefault("stage_progress", {})
    sp = stage_prog.setdefault(sid, {})
    sp["status"] = "approved"
    sp["approved_at"] = now

    log = state.setdefault("approval_log", [])
    log.append({
        "stage_id": sid,
        "approved_at": now,
        "approval_method": method,
        "marker": marker,
    })

    state["approved_through_stage"] = sid
    state["awaiting_stage_approval"] = None
    state["stuck_counter"] = 0
    return state


# ---------------------------------------------------------------------------
# §5.1 v3 file layout — loaders
# ---------------------------------------------------------------------------
#
# v3 collapses 14 files per task into 9. The new author files are:
#   goal.md, plan.yaml, README.md, run.py
# The new state files are:
#   state/state.json, state/draft_findings.md,
#   state/exploration_log.jsonl (+ auto-written current_step.md, inbox.md,
#   recap.md after first cycle).
#
# `state.json` consolidates the v2 plan_state.json + task_state.json +
# approval_log.yaml + email_thread_id.txt structured fields. Findings
# prose is NOT in this JSON — it lives in `state/draft_findings.md`
# (overwritten each cycle, latest-only).

_KV_LINE_RE = re.compile(r"^\s*[-*]\s+\*\*([^*]+?)\*\*\s*:?\s*(.+?)\s*$")


def _extract_scope_subsections(scope_body: str) -> tuple[list[str], list[str]]:
    """Parse the `## Scope` H2 body, splitting on `### In scope` /
    `### Out of scope` H3 headings. Returns (in_scope, out_of_scope) bullet
    lists. Bullets within each H3 subsection are extracted via
    `_extract_bullets`."""
    in_scope: list[str] = []
    out_scope: list[str] = []
    current: Optional[str] = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal in_scope, out_scope, current_lines
        text = "\n".join(current_lines)
        if current == "in":
            in_scope.extend(_extract_bullets(text))
        elif current == "out":
            out_scope.extend(_extract_bullets(text))
        current_lines = []

    for line in scope_body.splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            _flush()
            title = m.group(1).strip().lower()
            if title.startswith("in"):
                current = "in"
            elif title.startswith("out"):
                current = "out"
            else:
                current = None
        else:
            current_lines.append(line)
    _flush()
    return in_scope, out_scope


def _parse_kv_list(section_body: str) -> dict:
    """Parse a bullet list of `- **key:** value` lines into a flat dict.
    Values are coerced to int / float / bool / None when possible; backticked
    values keep the backticks stripped; everything else stays as the trimmed
    string."""
    out: dict = {}
    for line in section_body.splitlines():
        m = _KV_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1).strip().rstrip(":").strip().lower().replace(" ", "_")
        raw = m.group(2).strip()
        # Strip surrounding backticks.
        if len(raw) >= 2 and raw[0] == raw[-1] == "`":
            raw = raw[1:-1]
        lower = raw.lower()
        if lower in ("true", "yes"):
            value: Any = True
        elif lower in ("false", "no"):
            value = False
        elif lower in ("null", "none", ""):
            value = None
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        out[key] = value
    return out


def _parse_methodology_pipeline(section_body: str) -> list[dict]:
    """Parse `## Methodology` bullets of shape `- **P0** <title>: <body>` into
    [{phase, title, description}]. Unmatched bullets become {description}-only
    entries so the parser stays forgiving for sparsely-filled templates."""
    out: list[dict] = []
    line_re = re.compile(r"^\s*[-*]\s+\*\*(?P<phase>[^*]+)\*\*\s*(?P<title>[^:]*):\s*(?P<body>.*)$")
    plain_bullet_re = re.compile(r"^\s*[-*]\s+(?P<body>.+)$")
    for line in section_body.splitlines():
        m = line_re.match(line)
        if m:
            out.append({
                "phase": m.group("phase").strip(),
                "title": m.group("title").strip(),
                "description": m.group("body").strip(),
            })
            continue
        m2 = plain_bullet_re.match(line)
        if m2:
            out.append({"phase": "", "title": "", "description": m2.group("body").strip()})
    return out


def load_goal_md(task_dir: Path) -> dict:
    """Parse `goal.md` into a structured dict.

    Returns: {goal, things_to_show, scope_in, scope_out, success_criteria,
              methodology_pipeline, orchestrator, experiment_config,
              raw_body, path}. Missing sections become empty strings/lists.

    Raises FileNotFoundError when the file is absent — the runner's bootstrap
    responsibility is to fail clearly and point the user at the scaffolder.
    """
    path = task_dir / "goal.md"
    if not path.exists():
        raise FileNotFoundError(
            f"goal.md not found at {path}. "
            "Scaffold a new task with: "
            "steward-plan-runner init <task_id> --experiment-repo <abs_path> "
            "--email-prefix \"[TAG]\""
        )
    body = path.read_text(encoding="utf-8")
    sections = _split_h2_sections(body)

    goal = sections.get("goal", "").strip()
    things = _extract_bullets(sections.get("things to show", ""))

    scope_body = sections.get("scope", "")
    scope_in, scope_out = _extract_scope_subsections(scope_body)

    success = _extract_bullets(sections.get("success criteria", ""))
    methodology = _parse_methodology_pipeline(sections.get("methodology", ""))
    orchestrator = _parse_kv_list(sections.get("orchestrator", ""))
    exp_config = _parse_kv_list(sections.get("experiment config", ""))

    return {
        "goal": goal,
        "things_to_show": things,
        "scope_in": scope_in,
        "scope_out": scope_out,
        "success_criteria": success,
        "methodology_pipeline": methodology,
        "orchestrator": orchestrator,
        "experiment_config": exp_config,
        "raw_body": body,
        "path": str(path),
    }


def _default_state_json(plan: Optional[dict] = None) -> dict:
    """Return the empty v3 state.json skeleton seeded with pending entries
    for every plan step and stage. Findings prose is NOT in this dict —
    that lives in state/draft_findings.md."""
    step_progress: dict[str, dict] = {}
    if plan:
        for step in plan.get("steps") or []:
            step_progress[step["id"]] = {
                "status": "pending",
                "attempts": 0,
                "completed_at": None,
                "last_results_path": None,
                "deepening_cycles_done": 0,
                "deepening_actions_generated": [],
            }
        for action in plan.get("deepening_added_by_runner") or []:
            step_progress.setdefault(action["id"], {
                "status": "pending",
                "attempts": 0,
                "completed_at": None,
                "last_results_path": None,
                "deepening_cycles_done": 0,
                "deepening_actions_generated": [],
            })

    stage_progress: dict[str, dict] = {}
    stages = (plan or {}).get("stages") or []
    if not stages and plan:
        stages = [{"id": "all", "steps": [s["id"] for s in plan.get("steps") or []]}]
    for st in stages:
        stage_progress[st["id"]] = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "approved_at": None,
            "steps_done": 0,
            "steps_total": len(st.get("steps") or []),
        }

    return {
        "schema_version": 1,
        "plan_version": (plan or {}).get("version", 1),
        "plan_started_at": (plan or {}).get("approved_at"),
        "cooldown": {
            "last_cycle_time": None,
            "cycle": 0,
            "days_since_reply": 0,
        },
        "step_progress": step_progress,
        "stage_progress": stage_progress,
        "awaiting_stage_approval": None,
        "pending_intent_question": None,
        "weaknesses_log": [],
        "approval_log": [],
        "approved_through_stage": None,
        "email_thread_id": "",
        "last_message_id": "",
        "stuck_counter": 0,
        "max_stuck_cycles": 5,
    }


def load_state_json(state_dir: Path, plan: Optional[dict] = None) -> dict:
    """Read `state/state.json`. Bootstraps from `_default_state_json(plan)`
    when missing. Backfills any keys/steps/stages that joined the plan since
    the last save so older state files keep working."""
    path = state_dir / "state.json"
    data = _load_json_file(path)
    defaults = _default_state_json(plan)
    if not isinstance(data, dict):
        return defaults

    # Top-level keys.
    for key, default_value in defaults.items():
        data.setdefault(key, default_value)

    # Backfill missing nested cooldown keys.
    for k, v in defaults["cooldown"].items():
        data["cooldown"].setdefault(k, v)

    # Backfill step_progress entries for newly-added steps.
    if plan:
        for step in plan.get("steps") or []:
            sid = step["id"]
            if sid not in data["step_progress"]:
                data["step_progress"][sid] = defaults["step_progress"][sid]
        for action in plan.get("deepening_added_by_runner") or []:
            aid = action["id"]
            if aid not in data["step_progress"]:
                data["step_progress"][aid] = {
                    "status": "pending",
                    "attempts": 0,
                    "completed_at": None,
                    "last_results_path": None,
                    "deepening_cycles_done": 0,
                    "deepening_actions_generated": [],
                }
        # Backfill stage_progress for any newly-added stages.
        for sid, default_sp in defaults["stage_progress"].items():
            if sid not in data["stage_progress"]:
                data["stage_progress"][sid] = default_sp
            else:
                for k, v in default_sp.items():
                    data["stage_progress"][sid].setdefault(k, v)

    return data


def save_state_json(state_dir: Path, state: dict) -> None:
    """Atomic write of `state/state.json`."""
    state_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=False)
    _atomic_write(state_dir / "state.json", body + "\n")


def load_inbox_md(state_dir: Path) -> str:
    """Read `state/inbox.md`. Returns "" when missing."""
    path = state_dir / "inbox.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_draft_findings_md(state_dir: Path) -> str:
    """Read `state/draft_findings.md`. Returns "" when missing."""
    path = state_dir / "draft_findings.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §5.2 v3 file layout — writers
# ---------------------------------------------------------------------------

def write_inbox_md(state_dir: Path, feedback: Optional[dict]) -> None:
    """Dump the latest user reply to state/inbox.md (or a sentinel if empty).
    Includes any parsed approval/answer markers as a header. Cap body to
    first 200 lines."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if not feedback:
        body = "(no new mail since last cycle)\n"
        _atomic_write(state_dir / "inbox.md", body)
        return

    text = feedback.get("body", "") or ""
    markers: list[str] = []
    for m in APPROVAL_MARKER_RE.finditer(text):
        markers.append(f"approve: {m.group(0).strip()}")
    for m in ANSWER_MARKER_RE.finditer(text):
        markers.append(f"answer: {m.group(0).strip()}")
    header = ""
    if markers:
        header = "## Markers parsed\n" + "\n".join(f"- {x}" for x in markers) + "\n\n"
    text_capped = "\n".join(text.splitlines()[:200])
    body = header + "## Body\n\n" + text_capped + "\n"
    _atomic_write(state_dir / "inbox.md", body)


def write_current_step_md(
    state_dir: Path, plan: dict, state: dict, action: dict
) -> None:
    """Write THIS cycle's step to state/current_step.md. Empty body if
    action.kind isn't a step dispatch (plan_step / deepening)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if action.get("kind") not in ("plan_step", "deepening"):
        body = f"(no step dispatched this cycle: {action.get('kind')})\n"
        _atomic_write(state_dir / "current_step.md", body)
        return

    step = action.get("step") or action.get("action") or {}
    sid = step.get("id", "?")
    progress = state.get("step_progress", {}).get(sid, {})
    intent = state.get("pending_intent_question")
    intent_block = ""
    if intent and intent.get("step_id") == sid:
        intent_block = (
            "\n## Pending intent question (you asked last cycle)\n\n"
            f"> {intent['question']}\n\n"
            f"Asked at: {intent.get('asked_at')}\n"
            "User answer (if any) is in state/inbox.md.\n"
        )

    sc = step.get("success_criteria") or {}
    hard_block = (
        "\n".join("- " + h for h in (sc.get("hard") or []))
        if sc.get("hard")
        else "(none)"
    )
    soft_block = ((sc.get("soft") or {}).get("rubric") or "(none)").strip()

    body = f"""# Step: {sid}

## Description

{(step.get('description') or '').strip()}

## Success criteria

### Hard (deterministic)
{hard_block}

### Soft (LLM-judged)
{soft_block}

## Notes

{(step.get('notes') or '(none)').strip()}

## Where to write your results

`<experiment_repo>/results/{sid}_results.json`

## Prior attempts on this step

attempts so far: {progress.get('attempts', 0)}
status: {progress.get('status', 'pending')}
{intent_block}"""
    _atomic_write(state_dir / "current_step.md", body)


def write_recap_md(state_dir: Path, plan: dict, state: dict, goal: dict) -> None:
    """Write the per-cycle checklist + findings + on-demand index to
    state/recap.md."""
    state_dir.mkdir(parents=True, exist_ok=True)
    cycle = state.get("cooldown", {}).get("cycle", 0)
    today = datetime.now().strftime("%Y-%m-%d")
    progress = state.get("step_progress", {})

    lines = [f"# Recap — cycle {cycle} — {today}", ""]

    # Step checklist, grouped by stage.
    lines.append("## Step checklist")
    stages = plan.get("stages") or [
        {"id": "all", "steps": [s["id"] for s in plan.get("steps") or []]}
    ]
    icon_map = {
        "completed": "[x]",
        "done_enough": "[x]",
        "deepening": "[~]",
        "in_progress": "[>]",
        "failed": "[!]",
    }
    for stage in stages:
        sp = state.get("stage_progress", {}).get(stage["id"], {})
        lines.append(f"\n### Stage: {stage['id']} ({sp.get('status', 'pending')})")
        for sid in stage["steps"]:
            p = progress.get(sid, {})
            icon = icon_map.get(p.get("status"), "[ ]")
            done = p.get("completed_at", "") or ""
            suffix = f"  {done}" if done else ""
            lines.append(f"- {icon} {sid}{suffix}")
    lines.append("")

    # Findings (load the latest single section from draft_findings.md).
    lines.append("## Findings to date (cumulative across past cycles → see email thread; this cycle's draft below)")
    draft = load_draft_findings_md(state_dir).strip()
    lines.append(draft or "_(no draft findings yet — runner will write some after the first completed step)_")
    lines.append("")

    # On-demand index.
    lines.append("## On-demand index — read these only when you need them")
    lines.append("- **plan.yaml** — full step DAG, stages, dependencies, future steps, deepening queue")
    lines.append("- **state/state.json** — full machine state: every prior attempt's stdout/stderr tail, weaknesses log, approval log, cost-USD per step, intent-question history")
    lines.append("- **state/exploration_log.jsonl** — raw dispatch log (one line per cycle)")
    lines.append("- **state/exploration_archive_<YYYY-MM>.jsonl** — older dispatch entries rotated out of the working log")
    lines.append("- **state/weaknesses_archive_<YYYY-MM>.jsonl** — older reviewer-concern entries rotated out of state.json::weaknesses_log")
    lines.append("- **`<experiment_repo>/results/<step_id>_results.json`** — full results for any completed step")
    lines.append("")

    _atomic_write(state_dir / "recap.md", "\n".join(lines))


def write_draft_findings_md(state_dir: Path, body: str) -> None:
    """Atomic OVERWRITE of state/draft_findings.md with this cycle's
    findings only. No accumulation, no append. The previous cycle's
    findings are discarded — they live in past email threads."""
    state_dir.mkdir(parents=True, exist_ok=True)
    header = "# Findings draft (runner-written, awaiting user approval)\n"
    timestamp = f"\n_drafted: {datetime.now().isoformat(timespec='seconds')}_\n\n"
    _atomic_write(
        state_dir / "draft_findings.md",
        header + timestamp + (body or "").rstrip() + "\n",
    )


def compute_stage_progress_banner(plan: dict, state: dict, action: dict) -> str:
    """Return the §2.6 banner string. Pure function; no LLM, no I/O.

    Format: "**Stage progress.** Stage `<id>` (<N>/<M> steps done) — <summary>"
    where <summary> reflects the cycle action.
    """
    stages = plan.get("stages") or []
    if not stages:
        return "**Stage progress.** (no stages defined)"

    stage_prog = state.get("stage_progress", {}) or {}
    active = None
    for st in stages:
        sp = stage_prog.get(st["id"], {})
        if sp.get("status") != "approved":
            active = st
            break
    if active is None:
        last = stages[-1]
        n = len(last.get("steps") or [])
        return (
            f"**Stage progress.** Stage `{last['id']}` ({n}/{n} steps done) — "
            "all stages approved, plan complete"
        )

    sp = stage_prog.get(active["id"], {})
    n_total = sp.get("steps_total")
    if not n_total:
        n_total = len(active.get("steps") or [])
    # Recompute steps_done from step_progress so the banner can't drift
    # if the runner forgot to bump steps_done this cycle.
    step_prog = state.get("step_progress", {}) or {}
    done_states = ("completed", "done_enough")
    n_done = sum(
        1 for sid in (active.get("steps") or [])
        if step_prog.get(sid, {}).get("status") in done_states
    )
    if n_done < sp.get("steps_done", 0):
        n_done = sp.get("steps_done", 0)
    prefix = f"**Stage progress.** Stage `{active['id']}` ({n_done}/{n_total} steps done)"

    kind = action.get("kind", "")
    if kind == "plan_step":
        sid = (action.get("step") or {}).get("id", "?")
        return f"{prefix} — running step `{sid}`"
    if kind == "deepening":
        act = action.get("action") or {}
        sid = act.get("parent_step_id") or act.get("id") or "?"
        return f"{prefix} — deepening `{sid}`"
    if kind == "awaiting_stage_approval":
        ids = [s["id"] for s in stages]
        try:
            nxt = ids[ids.index(active["id"]) + 1]
        except (ValueError, IndexError):
            nxt = "(none)"
        return f"{prefix} — awaiting approval to advance to `{nxt}`"
    if kind == "intent_pending":
        return f"{prefix} — idle — awaiting answer to intent question"
    if kind == "stuck":
        return f"{prefix} — idle — stuck ({action.get('reason', '')})"
    if kind == "all_done":
        return (
            f"{prefix} — all_done — plan exhausted, awaiting human to add "
            "new steps to plan.yaml"
        )
    if kind in ("awaiting_approval",):  # legacy v2 kind, surfaced verbatim
        return f"{prefix} — awaiting approval ({action.get('reason', '')})"
    return f"{prefix} — idle — {kind or 'unknown'}"


def rotate_exploration_log_if_needed(state_dir: Path, cap: int = 50) -> None:
    """When state/exploration_log.jsonl exceeds `cap` entries, move the
    oldest entries (everything before the last `cap`) into a per-month
    archive file and rewrite the working file to keep only the last `cap`."""
    path = state_dir / "exploration_log.jsonl"
    if not path.exists():
        return
    raw = path.read_text(encoding="utf-8").splitlines()
    lines = [l for l in raw if l.strip()]
    if len(lines) <= cap:
        return
    archive_idx = len(lines) - cap
    archived, kept = lines[:archive_idx], lines[archive_idx:]
    archive_name = f"exploration_archive_{datetime.now().strftime('%Y-%m')}.jsonl"
    archive_path = state_dir / archive_name
    with archive_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(archived) + "\n")
    _atomic_write(path, "\n".join(kept) + "\n")


def cap_weaknesses_log(state: dict, state_dir: Path, cap: int = 20) -> None:
    """In-place cap of state['weaknesses_log']. Drops oldest entries into a
    per-month JSONL archive. Call BEFORE save_state_json."""
    log = list(state.get("weaknesses_log") or [])
    if len(log) <= cap:
        state["weaknesses_log"] = log
        return
    archived = log[:-cap]
    kept = log[-cap:]
    archive_name = f"weaknesses_archive_{datetime.now().strftime('%Y-%m')}.jsonl"
    archive_path = state_dir / archive_name
    state_dir.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as f:
        for entry in archived:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    state["weaknesses_log"] = kept


# ---------------------------------------------------------------------------
# §4.11 Autonomous dispatcher (Codex CLI default; opt-in per task)
#
# Library default for steps that lack `run_command`. The plan_runner runtime
# (each task's `run.py`) calls `default_codex_dispatcher` when
# `definition.yaml::experiment_config.llm_dispatch_when_no_run_command` is
# truthy and the step has no `run_command`. The dispatcher spawns the Codex
# CLI in `workspace-write` sandbox at the experiment_repo, gives the
# sub-agent the step's description + success_criteria, and lets it write
# code and produce `results/<step_id>_results.json`.
#
# Conventions baked into the prompt:
#   - Sub-agent commits its changes per subfolder (where each subfolder is
#     its own git repo) but NEVER pushes. The orchestrator surfaces diffs
#     in the cycle email; the user pushes manually.
#   - On a real intent/design question, the sub-agent emits
#     `INTENT_NEEDED: <one-line question>` to stdout and exits non-zero.
#     The runtime captures this, stores it in
#     `plan_state.pending_intent_question`, sets `awaiting_approval`, and
#     surfaces the question in the cycle email. The user answers via
#     `/answer <text>` (email reply) OR by editing
#     `plan.yaml::steps[<id>].notes` and bumping plan version.
#   - Routine micro-decisions (random seed, default model, output sub-path)
#     are NOT escapes; the sub-agent picks a reasonable default and logs
#     it to `results/<step_id>_results.json::decisions_made: {...}`.
#   - Locked artifacts (`definition.yaml`, `project_state.md`) are
#     read-only — the prompt tells the agent explicitly.
#
# Budget: best-effort. `experiment_config.max_step_cost_usd` (default 30)
# is passed to Codex as a session hint and parsed back from `--json` token
# events if available. Hard enforcement is wall-clock via
# `step_timeout_seconds`.
# ---------------------------------------------------------------------------

INTENT_MARKER_RE = re.compile(r"^\s*INTENT_NEEDED:\s*(.+?)\s*$", re.MULTILINE)
ANSWER_MARKER_RE = re.compile(
    r"^\s*/answer(?:\s+(\d+))?\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _find_git_subdirs(experiment_repo: Path) -> list[Path]:
    """Return one-level-deep subdirectories of experiment_repo that are git repos.

    Used by the dispatcher to commit changes per-subfolder. If experiment_repo
    itself is a git repo, returns [experiment_repo] only — the "multi-repo
    parent" pattern is detected by the presence of .git in subfolders.
    """
    if not experiment_repo.exists():
        return []
    self_git = (experiment_repo / ".git").exists()
    sub_repos: list[Path] = []
    for child in sorted(experiment_repo.iterdir()):
        if not child.is_dir():
            continue
        if (child / ".git").exists():
            sub_repos.append(child)
    if sub_repos:
        return sub_repos
    if self_git:
        return [experiment_repo]
    return []


def _git_current_branch(repo: Path) -> Optional[str]:
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        return None


def _commit_dirty_subdirs(
    experiment_repo: Path,
    step_id: str,
    cycle: int,
) -> list[dict]:
    """Walk subfolders, commit dirty ones with a step-prefixed message.

    Returns [{"subdir": str, "branch": str, "sha": str, "files_changed": int}].
    NEVER pushes. NEVER amends. Each commit is fresh.
    """
    import subprocess
    out: list[dict] = []
    for repo in _find_git_subdirs(experiment_repo):
        try:
            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True, text=True, timeout=20,
            )
            if status.returncode != 0 or not status.stdout.strip():
                continue
            files_changed = len([l for l in status.stdout.splitlines() if l.strip()])
            add = subprocess.run(
                ["git", "-C", str(repo), "add", "-A"],
                capture_output=True, text=True, timeout=30,
            )
            if add.returncode != 0:
                out.append({
                    "subdir": str(repo), "branch": _git_current_branch(repo),
                    "sha": None, "files_changed": files_changed,
                    "error": f"git add failed: {add.stderr[:200]}",
                })
                continue
            msg = f"plan_runner: {step_id} (cycle {cycle})"
            commit = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", msg],
                capture_output=True, text=True, timeout=30,
            )
            if commit.returncode != 0:
                # Could be empty-after-staging — record but don't fail the step.
                out.append({
                    "subdir": str(repo), "branch": _git_current_branch(repo),
                    "sha": None, "files_changed": files_changed,
                    "error": f"git commit failed: {commit.stderr[:200]}",
                })
                continue
            sha_proc = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            out.append({
                "subdir": str(repo),
                "branch": _git_current_branch(repo),
                "sha": sha_proc.stdout.strip() if sha_proc.returncode == 0 else None,
                "files_changed": files_changed,
            })
        except Exception as e:
            out.append({
                "subdir": str(repo), "branch": _git_current_branch(repo),
                "sha": None, "files_changed": -1, "error": str(e)[:200],
            })
    return out


def _build_codex_prompt(
    task_dir: Path,
    experiment_repo: Path,
    intent_answer: Optional[str] = None,
) -> str:
    """Build the Codex sub-agent prompt for the v3.1 4-file contract.

    The agent reads its task description from ``state/current_step.md``
    (which the runner has written before invoking the dispatcher), not
    from prompt arguments. Per-step description, success criteria, and
    prior-attempt notes all live in that file. The prompt's only job is
    to enforce the 4-default-files contract, the locked-folder boundary,
    and the intent-escape grammar.
    """
    sub_repos = _find_git_subdirs(experiment_repo)
    sub_repo_lines = []
    for r in sub_repos:
        br = _git_current_branch(r) or "?"
        sub_repo_lines.append(f"  - {r.name}/ (branch: {br})")
    sub_repo_block = "\n".join(sub_repo_lines) if sub_repo_lines else (
        f"  - {experiment_repo} is the only repo (no subfolders)"
    )

    intent_block = ""
    if intent_answer:
        intent_block = (
            "\n## User answer to your previous INTENT_NEEDED question\n\n"
            f"The user replied: {intent_answer!r}\n"
            "Incorporate this answer and proceed. Do not re-ask the same question.\n"
        )

    return f"""You are a plan_runner sub-agent executing ONE plan step.

## Read these 4 files now (and only these by default)

1. `{task_dir}/goal.md`                — locked project goal + methodology
2. `{task_dir}/state/current_step.md`  — THIS cycle's step (your full task)
3. `{task_dir}/state/inbox.md`         — user's latest reply (or "(no new mail)")
4. `{task_dir}/state/recap.md`         — checklist + findings + on-demand index

The on-demand index in `recap.md` tells you what other files exist and when
to read them. Do NOT load anything else by default — wasted context slows
the cycle. Read additional files only when `recap.md::On-demand index` points
you at them AND you actually need them for this step.

## Your task

Your full task description, success criteria, and prior-attempt notes are
in `{task_dir}/state/current_step.md`. Read it. Do the work. Write the
results JSON. Commit per-subfolder. Exit.

The results file path is given in `current_step.md` under "Where to write
your results". The `metric:` lines in the hard success criteria reference
dotted keys inside that JSON — make sure they resolve.

## Where you work

Experiment repo root: {experiment_repo}
Subfolders (each is its own git repo on the listed branch):
{sub_repo_block}

The locked task-folder is at {task_dir} — read-only. You must NOT modify
any file inside it (`goal.md`, `plan.yaml`, `README.md`, `run.py`, or
anything under `state/`).

## Rules you MUST follow

1. **Commit, do NOT push.** After producing the results JSON, the runtime
   will auto-commit your changes per-subfolder with message
   `plan_runner: <step_id>` after you exit. You may make your own commits
   too, but **never run `git push`**.
2. **Locked files are read-only.** Never modify the task folder
   ({task_dir}) — no edits to `goal.md`, `plan.yaml`, `README.md`, `run.py`,
   or anything in `state/`.
3. **Intent escape.** If a real design/intent question blocks you
   (something that would change the headline result direction, not a
   routine seed/model choice), STOP and write
   `INTENT_NEEDED: <one-line question>` to stdout, then exit. The user
   will reply with `/answer <text>` (email) or edit `plan.yaml::steps`
   notes; you will rerun next cycle with the answer in context.
4. **Routine micro-decisions** (random seed, default model, output
   sub-path) are NOT escapes. Pick a reasonable default and log it to
   `results/<step_id>_results.json::decisions_made: {{...}}`.
5. **Stay on-task.** One step, one results file, exit. Do not extend
   scope. Do not add steps. Do not refactor unrelated code.
6. **Be concrete.** Run code. Verify your results file exists and the
   hard-criteria keys resolve before exiting.
{intent_block}
## Now begin.
"""


def _parse_codex_intent(stdout: str, stderr: str) -> Optional[str]:
    """Extract the first INTENT_NEEDED line from stdout/stderr."""
    for stream in (stdout or "", stderr or ""):
        m = INTENT_MARKER_RE.search(stream)
        if m:
            return m.group(1).strip()
    return None


def _parse_codex_cost(stdout: str, stderr: str) -> Optional[float]:
    """Best-effort cost extraction from Codex --json events.

    Codex emits a final-usage JSON event on stdout when --json is set.
    We grep for total_cost / cost_usd / total_tokens. Returns None if
    nothing is parseable (the runtime falls back to wall-clock only).
    """
    text = (stdout or "") + "\n" + (stderr or "")
    # Try multiple patterns; Codex's exact field name has changed across
    # versions. First match wins.
    for pat in (
        r'"total_cost_usd"\s*:\s*([0-9.]+)',
        r'"cost_usd"\s*:\s*([0-9.]+)',
        r'"total_cost"\s*:\s*([0-9.]+)',
        r'total cost[:\s]+\$?([0-9.]+)',
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                continue
    return None


def default_codex_dispatcher(
    step: dict,
    definition: dict,
    experiment_repo: Path,
    task_dir: Path,
    cycle: int,
    step_timeout_seconds: int,
    *,
    intent_answer: Optional[str] = None,
    project_state: Optional[dict] = None,
    sandbox: str = "workspace-write",
    model: Optional[str] = None,
) -> dict:
    """Spawn Codex CLI to execute a plan step autonomously.

    Use this when `step.run_command` is unset and your task's definition
    sets `experiment_config.llm_dispatch_when_no_run_command: true`. The
    sub-agent reads `step.description` + `success_criteria`, writes code,
    runs it, and produces `experiment_repo/results/<step_id>_results.json`.

    Returns:
        dict with keys: status ("success" | "failed" | "timeout" |
        "intent_needed" | "codex_unavailable"), stdout, stderr, returncode,
        intent_question (Optional[str]), cost_usd (Optional[float]),
        commits (list[dict]), step_id, sandbox, model.

    Side effects:
        - Spawns `codex exec` in subprocess with `-s workspace-write` and
          `-C <experiment_repo>`.
        - On non-error exit, walks experiment_repo subfolders and commits
          dirty ones (one commit per dirty subfolder). Does NOT push.

    Failure modes:
        - codex not on PATH → returns status="codex_unavailable".
        - Wall-clock timeout → status="timeout".
        - INTENT_NEEDED on stdout → status="intent_needed", intent_question set.
        - Non-zero exit with no intent marker → status="failed".
    """
    import shutil
    import subprocess

    step_id = step["id"]
    if shutil.which("codex") is None:
        return {
            "status": "codex_unavailable",
            "stdout": "", "stderr": "codex CLI not found on PATH",
            "returncode": None, "intent_question": None,
            "cost_usd": None, "commits": [], "step_id": step_id,
            "sandbox": sandbox, "model": model,
        }

    prompt = _build_codex_prompt(
        task_dir=task_dir,
        experiment_repo=experiment_repo,
        intent_answer=intent_answer,
    )

    # Build codex argv. Note: experiment_repo may not be a git repo itself
    # (the err_loc setup has three sub-repos under a non-git parent), so we
    # pass --skip-git-repo-check.
    cmd = ["codex", "exec", "-s", sandbox, "-C", str(experiment_repo),
           "--skip-git-repo-check", "--json", "-"]
    if model:
        cmd.extend(["-m", model])

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=step_timeout_seconds,
        )
        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        status_raw = "ran"
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = (e.stderr or "") + f"\n[dispatcher] timed out after {step_timeout_seconds}s"
        returncode = None
        status_raw = "timeout"

    intent_question = _parse_codex_intent(stdout, stderr)
    cost_usd = _parse_codex_cost(stdout, stderr)

    if status_raw == "timeout":
        final_status = "timeout"
    elif intent_question:
        final_status = "intent_needed"
    elif returncode == 0:
        final_status = "success"
    else:
        final_status = "failed"

    commits: list[dict] = []
    # Commit on success OR partial-success (even if codex exits non-zero, it
    # may have left useful artifacts). Skip on intent_needed (preserve state)
    # and on timeout (state is ambiguous).
    if final_status == "success":
        try:
            commits = _commit_dirty_subdirs(experiment_repo, step_id, cycle)
        except Exception as e:
            stderr += f"\n[dispatcher] auto-commit failed: {e}"

    return {
        "status": final_status,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "intent_question": intent_question,
        "cost_usd": cost_usd,
        "commits": commits,
        "step_id": step_id,
        "sandbox": sandbox,
        "model": model,
    }


def check_intent_answer(
    state_dir: Path,
    plan: dict,
    feedback: Optional[dict] = None,
    plan_state: Optional[dict] = None,
) -> Optional[str]:
    """Look for an answer to a pending INTENT_NEEDED question.

    Sources, in order:
      1. `feedback.body` / `feedback.replies[].body` containing
         `/answer <text>` (case-insensitive multiline regex).
      2. `plan.yaml::steps[<id>].notes` for the pending step_id, when
         that notes block contains an `/answer <text>` marker. Same
         regex grammar as the email channel so the user can answer
         either way with identical syntax. Requires `plan_state` to
         locate the pending step_id.
    Returns the answer text or None.
    """
    candidate_texts: list[str] = []
    if isinstance(feedback, dict):
        if isinstance(feedback.get("body"), str):
            candidate_texts.append(feedback["body"])
        for r in feedback.get("replies") or []:
            if isinstance(r, dict) and isinstance(r.get("body"), str):
                candidate_texts.append(r["body"])

    if plan_state and isinstance(plan_state.get("pending_intent_question"), dict):
        pending_sid = plan_state["pending_intent_question"].get("step_id")
        if pending_sid and isinstance(plan, dict):
            for s in (plan.get("steps") or []):
                if s.get("id") == pending_sid:
                    notes = s.get("notes")
                    if isinstance(notes, str) and notes.strip():
                        candidate_texts.append(notes)
                    break

    for text in candidate_texts:
        m = ANSWER_MARKER_RE.search(text)
        if m:
            return (m.group(2) or "").strip()
    return None
