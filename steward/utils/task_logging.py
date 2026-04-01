"""Shared logging utilities for tasks."""

import logging
import sys
from pathlib import Path


def setup_task_logger(task_id: str, level: int = logging.INFO) -> logging.Logger:
    """Set up a logger for a specific task."""
    logger = logging.getLogger(task_id)
    logger.setLevel(level)

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        f"[{task_id}] %(asctime)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger
