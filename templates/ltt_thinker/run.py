#!/usr/bin/env python3
"""LTT task — single-project proactive research cycle using CycleRunner.

Drop-in template. Only definition.yaml and state/ differ per project.
Override plan() or register custom actions for experiment dispatch, etc.

Usage:
    python run.py                          # run one cycle
    python run.py --summary-dir /path      # explicit summary output
"""

import os
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
REPO_ROOT = TASK_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("CHARTER_INSTANCE_ROOT", str(REPO_ROOT))

from charter_worker.runner import CycleRunner
from charter_worker.actions import Action


class LTTThinker(CycleRunner):
    """Proactive research thinker — searches, synthesizes, emails, speculates.

    Customize by overriding plan() to add experiment actions, budget rotation,
    or other task-specific logic.
    """

    def plan(self, context):
        """Default plan: web search from open questions."""
        actions_config = self.definition.get("actions", {})
        status = context.get("status", {})
        open_questions = status.get("open_questions", [])

        actions = []

        # Web search
        ws_config = actions_config.get("web_search", {"enabled": True})
        if ws_config.get("enabled", True):
            if open_questions:
                q = open_questions[0]
                query = q.get("question", q) if isinstance(q, dict) else str(q)
            else:
                query = self.definition.get("goal", "")
            if query:
                actions.append(Action("lightweight_search", query=query, config=ws_config))

        # Experiment (if enabled and confidence high enough)
        exp_config = actions_config.get("experiment", {})
        if exp_config.get("enabled", False):
            confidence = status.get("confidence_score", 0)
            threshold = exp_config.get("confidence_threshold", 4)
            if confidence >= threshold:
                actions.append(Action("experiment", config=exp_config))

        return actions


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary-dir", default=None)
    parser.add_argument("--task-id", default=None)
    args = parser.parse_args()

    runner = LTTThinker(
        definition=TASK_DIR / "definition.yaml",
        state_dir=TASK_DIR / "state",
        summary_dir=args.summary_dir,
        task_id=args.task_id or TASK_DIR.name,
    )
    runner.run_cycle()


if __name__ == "__main__":
    main()
