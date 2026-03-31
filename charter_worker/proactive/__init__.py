"""Backwards-compatible re-exports.

Old import paths (from charter_worker.proactive.*) still work but
new code should use:
  - charter_worker.phases.*     (phase implementations)
  - charter_worker.llm          (CLI abstraction)
  - charter_worker.reflection.* (orchestrator self-improvement)
"""
