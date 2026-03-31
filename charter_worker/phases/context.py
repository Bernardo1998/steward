"""Phase 1 — Load Context.

Loads project state, checks for email replies, promotes speculative work,
detects stagnation.
"""

import json
import sys
from pathlib import Path
from typing import Optional

import yaml

import hashlib

from .gmail_reader import fetch_ltt_replies, save_attachments_to_context
from .guardrails import g7_stagnation_check, g10_parse_feedback, GuardrailResult

# Supported text file extensions for context_files
_TEXT_EXTENSIONS = {".md", ".txt", ".tex", ".py", ".yaml", ".yml", ".json", ".csv", ".bib", ".rst"}
_MAX_FILE_SIZE = 50_000  # 50KB per file
_MAX_TOTAL_CONTEXT = 100_000  # 100KB total context


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def _load_context_files(project_dir: Path) -> str:
    """Load user-provided context files from project_dir/context_files/.

    Reads text files directly. For large files or PDFs, generates and caches
    a summary in context_files/.summaries/.

    Returns a formatted string for injection into synthesis prompts.
    """
    ctx_dir = project_dir / "context_files"
    if not ctx_dir.exists():
        return ""

    summaries_dir = ctx_dir / ".summaries"
    summaries_dir.mkdir(exist_ok=True)

    sections = []
    total_size = 0

    # Sort files by modification time (newest first)
    files = sorted(ctx_dir.iterdir(), key=lambda f: f.stat().st_mtime if f.is_file() else 0, reverse=True)

    for file_path in files:
        if not file_path.is_file():
            continue
        if file_path.name.startswith("."):
            continue

        suffix = file_path.suffix.lower()

        # Check if we have a cached summary
        file_hash = hashlib.md5(f"{file_path.name}:{file_path.stat().st_mtime}".encode()).hexdigest()[:12]
        cache_path = summaries_dir / f"{file_path.stem}_{file_hash}.summary.txt"

        if cache_path.exists():
            content = cache_path.read_text(encoding="utf-8", errors="replace")
            sections.append(f"### {file_path.name} (cached summary)\n{content}")
            total_size += len(content)
        elif suffix in _TEXT_EXTENSIONS:
            file_size = file_path.stat().st_size
            if file_size <= _MAX_FILE_SIZE:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                sections.append(f"### {file_path.name}\n{content}")
                total_size += len(content)
            else:
                # File too large — read first + last portions, cache summary
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    head = f.read(_MAX_FILE_SIZE // 2)
                    f.seek(max(0, file_size - _MAX_FILE_SIZE // 4))
                    tail = f.read(_MAX_FILE_SIZE // 4)
                content = f"{head}\n\n[... {file_size - _MAX_FILE_SIZE * 3 // 4} bytes omitted ...]\n\n{tail}"
                cache_path.write_text(content, encoding="utf-8")
                sections.append(f"### {file_path.name} (truncated, {file_size} bytes)\n{content}")
                total_size += len(content)
        elif suffix == ".pdf":
            # PDF: note it exists but can't read inline
            sections.append(f"### {file_path.name} (PDF, {file_path.stat().st_size} bytes — drop a .txt or .md summary alongside it for best results)")
        else:
            sections.append(f"### {file_path.name} (unsupported format {suffix} — convert to .md or .txt)")

        if total_size >= _MAX_TOTAL_CONTEXT:
            sections.append(f"[... remaining files omitted, total context cap reached ...]")
            break

    if not sections:
        return ""

    return "\n\n".join(sections)


def _check_email_replies(project_id: str, project_dir: Path, state_meta: dict) -> tuple[Optional[str], list[dict]]:
    """Check Gmail IMAP for replies/messages for this project.

    Strategy: find the most recent message from the user (not the bot)
    matching the subject prefix, that arrived after last_email_date.
    No message-ID matching — this avoids breakage from manual sends,
    rate-limit retries, or thread mismatches.

    Returns:
        (reply_body_text, attachments_list)
    """
    last_email_date = state_meta.get("last_email_date")
    if not last_email_date:
        print(f"  [phase1] No previous email for {project_id}, skipping reply check", file=sys.stderr)
        return None, []

    # Determine subject prefix from state or default
    subject_prefix = state_meta.get("subject_prefix", "[LTT]")

    try:
        messages = fetch_ltt_replies(since_date=last_email_date, subject_prefix=subject_prefix)
    except Exception as e:
        print(f"  [phase1] IMAP error for {project_id}: {e}", file=sys.stderr)
        return None, []

    if not messages:
        return None, []

    # Filter: only messages FROM someone other than the bot (i.e., from the user)
    bot_address = ""
    try:
        from ..comm.email import _get_config_path
        import yaml as _yaml
        cfg_path = _get_config_path()
        if cfg_path.exists():
            with open(cfg_path) as _f:
                _cfg = _yaml.safe_load(_f)
            bot_address = _cfg.get("sender", {}).get("address", "").lower()
    except Exception:
        pass

    user_messages = []
    for msg in messages:
        from_addr = msg.get("from", "").lower()
        if bot_address and bot_address in from_addr:
            continue  # skip our own sent emails
        user_messages.append(msg)

    if not user_messages:
        return None, []

    # Return the most recent user message (last in list = most recent)
    latest = user_messages[-1]
    print(f"  [phase1] Found user message for {project_id}: {latest.get('subject', '')[:60]}", file=sys.stderr)
    return latest["body"], latest.get("attachments", [])


def _promote_speculative(
    buffer: dict,
    feedback: Optional[dict],
    status: dict,
) -> tuple[list[dict], list[str]]:
    """Promote validated speculative work into main status. Returns (promoted, discarded)."""
    threads = buffer.get("speculative_threads", [])
    promoted = []
    discarded = []

    for thread in threads:
        direction = thread.get("direction", "")
        findings = thread.get("preliminary_findings", "")

        # Promote if feedback aligns, or if findings seem self-validating
        should_promote = False
        if feedback:
            new_priorities = feedback.get("new_priorities", [])
            for p in new_priorities:
                if any(word in direction.lower() for word in p.lower().split()):
                    should_promote = True
                    break

        if should_promote:
            promoted.append({
                "finding": f"[PROMOTED FROM SPECULATION] {findings}",
                "provenance": "speculative_buffer",
                "relevance_score": 3,
                "added_cycle": status.get("cycle_number", 0) + 1,
            })
        else:
            discarded.append(direction)

    return promoted, discarded


def load_context(
    project_id: str,
    project_dir: Path,
    state_meta: dict,
) -> dict:
    """Load all project context for this cycle.

    Returns dict with:
        definition, status, exploration_log, feedback, promoted_findings,
        stagnation_result, guardrail_results
    """
    guardrail_results = []
    context_root = project_dir
    state_root = project_dir
    if project_dir.name == "state" and (project_dir.parent / "context_files").exists():
        context_root = project_dir.parent
    else:
        candidate_state = project_dir / "state"
        if candidate_state.exists():
            state_root = candidate_state

    # Load immutable definition
    definition = _load_yaml(context_root / "definition.yaml")

    # Load mutable status
    status = _load_yaml(state_root / "status.yaml")

    # Load exploration log
    exploration_log = _load_jsonl(state_root / "exploration_log.jsonl")

    # Load speculative buffer
    buffer = _load_yaml(state_root / "speculative_buffer.yaml")

    # Check for email reply (text + attachments)
    reply_text, reply_attachments = _check_email_replies(project_id, state_root, state_meta)

    # Save any attachments to context_files/
    if reply_attachments:
        saved = save_attachments_to_context(reply_attachments, context_root)
        for s in saved:
            print(f"  [phase1] Saved attachment: {s}", file=sys.stderr)

    # Parse feedback if reply exists
    feedback = None
    if reply_text:
        print(f"  [phase1] Parsing reply for {project_id}...", file=sys.stderr)
        feedback = g10_parse_feedback(reply_text, status, definition)
        state_meta["days_since_reply"] = 0

        # Apply corrections
        for correction in feedback.get("corrections", []):
            # Annotate exploration log
            exploration_log.append({
                "cycle": status.get("cycle_number", 0),
                "type": "correction",
                "original": correction.get("original", ""),
                "correction": correction.get("correction", ""),
                "source": "human_feedback",
            })

        # Mark rejected suggestions
        rejected = feedback.get("rejected_suggestions", [])
        for suggestion in status.get("action_suggestions", []):
            if suggestion.get("action", "") in rejected:
                suggestion["suppressed_until_cycle"] = status.get("cycle_number", 0) + 3

        # Inject new priorities as fresh open questions (high priority)
        new_priorities = feedback.get("new_priorities", [])
        if new_priorities:
            existing_questions = status.get("open_questions", [])
            current_cycle = status.get("cycle_number", 0)
            for priority in new_priorities:
                existing_questions.insert(0, {
                    "question": priority,
                    "priority": "high",
                    "added_cycle": current_cycle + 1,
                    "from_feedback": True,
                })
            # Keep max 5, but prioritize feedback-driven ones
            status["open_questions"] = existing_questions[:5]

        # Handle commands
        commands = feedback.get("commands", [])
        if "pause" in commands:
            status["paused"] = True
        if "done" in commands:
            status["completed"] = True

    else:
        # No reply — increment days_since_reply (but only if we've sent at least one email)
        if state_meta.get("last_email_date"):
            state_meta["days_since_reply"] = state_meta.get("days_since_reply", 0) + 1
        # else: first cycle, no email sent yet — keep days_since_reply at 0

    # Promote speculative work
    promoted_findings, discarded_specs = _promote_speculative(buffer, feedback, status)

    # Clear speculative buffer
    buffer_path = project_dir / "speculative_buffer.yaml"
    if buffer_path.exists():
        with open(buffer_path, "w") as f:
            yaml.dump({"speculative_threads": []}, f)

    # Stagnation detection (G7)
    metrics_history = state_meta.get("metrics_history", [])
    stagnation_result = g7_stagnation_check(metrics_history)
    guardrail_results.append(stagnation_result)
    if not stagnation_result.passed:
        status["needs_human_input"] = True
        if "human_input_questions" not in status:
            status["human_input_questions"] = []
        status["human_input_questions"].append(
            "This project appears stalled. Should I change approach, broaden scope, or pause?"
        )

    # Load context files
    context_files_summary = _load_context_files(context_root)
    if context_files_summary:
        file_count = context_files_summary.count("### ")
        print(f"  [phase1] Loaded {file_count} context file(s)", file=sys.stderr)

    return {
        "definition": definition,
        "status": status,
        "exploration_log": exploration_log,
        "feedback": feedback,
        "promoted_findings": promoted_findings,
        "stagnation_result": stagnation_result,
        "guardrail_results": guardrail_results,
        "days_since_reply": state_meta.get("days_since_reply", 0),
        "context_files_summary": context_files_summary,
    }
