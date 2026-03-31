"""Reusable workflow phases for iterative tasks.

Provides the 5-phase research cycle as importable functions.
Used directly by task run.py scripts or indirectly via CycleRunner.
"""

from .context import load_context
from .research import research, generate_queries
from .synthesize import synthesize
from .feedback import send_ltt_email
from .speculate import speculate
from .guardrails import GuardrailResult
