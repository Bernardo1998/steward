#!/usr/bin/env python3
"""CLI wrapper for the deep research utility.

Usage:
    python3 -m charter_worker.research.cli "What is the state of ...?"
    python3 -m charter_worker.research.cli --question "..." --output-dir ./out --max-workers 3

Exit codes:
    0 — success or partial
    1 — failed
"""

import argparse
import json
import sys
from pathlib import Path

from .engine import run_research


def main():
    parser = argparse.ArgumentParser(
        description="Deep Research — fan-out/fan-in via Codex CLI",
    )
    parser.add_argument(
        "question_pos", nargs="?", default=None,
        help="Research question (positional)",
    )
    parser.add_argument(
        "--question", dest="question_flag", default=None,
        help="Research question (named flag)",
    )
    parser.add_argument("--context", default="", help="Optional context string")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument(
        "--max-workers", type=int, default=5,
        help="Max parallel workers (default 5)",
    )
    parser.add_argument("--model", default=None, help="Codex model override")
    parser.add_argument(
        "--no-search", action="store_true",
        help="Disable --search on codex calls",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Print result JSON to stdout",
    )

    args = parser.parse_args()

    question = args.question_flag or args.question_pos
    if not question:
        parser.error("A research question is required (positional or --question)")

    result = run_research(
        question,
        context=args.context,
        output_dir=args.output_dir,
        max_workers=args.max_workers,
        model=args.model,
        search=not args.no_search,
    )

    if args.json_output:
        print(json.dumps(result, indent=2))

    status = result.get("status", "failed")
    return 0 if status in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
