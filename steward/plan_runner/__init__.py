"""steward.plan_runner — reusable plan-runner orchestrator.

Tasks construct a :class:`PlanRunner` in their `run.py` and call `.run()`
once per cycle. The library handles the full 17-phase cycle loop
(cooldown → load → inbox → stage approval → intent → select_next_action →
write recap/current_step → dispatch → judge → deepening → reviewer
scrutiny → draft findings → email → bookkeeping → summary).
"""

from .runner import PlanRunner
from .scaffolder import init_task

__all__ = ["PlanRunner", "init_task"]
