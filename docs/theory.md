# Organizational Theory Motivation

## The problem

When building an LLM-based system to handle a complex task, you face a fundamental
design choice: **should one agent do everything, or should you decompose the work
across multiple specialized agents?**

Current practice is ad-hoc. Multi-agent frameworks (LangGraph, AutoGen, CrewAI) make
it easy to spin up agent hierarchies, but provide no guidance on *when* decomposition
actually helps. Empirical studies consistently find that single-agent baselines match
or beat multi-agent systems on many benchmarks — yet certain tasks clearly benefit
from decomposition. The question is: **what structural properties of a task predict
whether adding agents helps?**

## The theory

This question was answered decades ago — for human organizations. Charter-worker draws
on three foundational theories:

### Coase: The default is centralized

Ronald Coase's *Theory of the Firm* (1937) asks: why do firms exist? Why not contract
everything on the open market? His answer: **transaction costs**. Coordinating across
organizational boundaries incurs search, negotiation, and enforcement overhead. You
internalize work when these coordination costs exceed the efficiency gains of
specialization.

**Applied to LLM agents:** Don't add agents unless the information-processing gain
from decomposition exceeds the coordination tax (context handoff, error propagation,
redundant computation). The single-agent is the default; multi-agent must justify itself.

### Simon: Decompose along natural joints

Herbert Simon's *Sciences of the Artificial* (1962) explains when decomposition works:
tasks with **near-decomposable** structure — subsystems that interact intensely
internally but weakly across boundaries. If you cut along these natural joints,
coordination costs stay low.

**Applied to LLM agents:** Decompose when the task has subtasks that are loosely
coupled — each subtask can be completed with minimal context from the others. If
subtasks share heavy context (long documents, conversation history, accumulated state),
keeping them in one agent avoids expensive handoffs.

### Galbraith: Match structure to information load

Jay Galbraith's *Organization Design* (1974) frames structure as an information-processing
problem. When uncertainty is low, simple hierarchies work. When uncertainty is high, you
need lateral relations, shared databases, or richer communication channels.

**Applied to LLM agents:** When the task is well-specified (clear inputs, deterministic
steps), a single agent or simple pipeline suffices. When the task involves ambiguity,
conflicting evidence, or creative exploration, richer agent structures (debate, voting,
iterative refinement) may be justified — but only if the added coordination channels
carry genuinely useful information.

## The architecture: charter-worker as testbed

Charter-worker's design reflects these principles pragmatically:

| Design choice | Theory | Rationale |
|--------------|--------|-----------|
| Tasks default to single-agent processes | Coase | Don't decompose until coordination gains are proven |
| Deep research engine fans out parallel workers | Simon | Subquestions are near-decomposable (each can be searched independently) |
| Experiment dispatch interleaves with research | Galbraith | Action selection adapts to information needs (search when uncertain, execute when ready) |
| Guardrails enforce long-horizon consistency | All three | Prevent the drift, stagnation, and boundary erosion that organizational theory predicts for unsupervised systems |
| Self-healing orchestrator diagnoses and fixes | Coase | Cheaper to internalize debugging than to escalate to a human every time |

## The experiment

We are running a controlled experiment to test these predictions directly:

**Four architecture arms** applied to the same set of tasks:
- **A**: Monolithic single agent
- **B**: Single agent + exact verifier overlay (one repair attempt)
- **C**: Serialized workflow in one agent (role tags, no separate state)
- **D**: Hierarchical multi-agent + verifier overlay

**Four task generator knobs** (independently varied):
- Decomposability (high/low)
- Coupling / repair pressure (high/low)
- Verifier availability (on/off)
- Context fragmentation (high/low)

**The prediction:** Architecture ranking should flip across regimes. Specifically:
- A→B gain (verification value) should be large when verifiers are available
- B→C gain (workflow logic) should be small in most regimes
- C→D gain (boundary value) should only be positive when decomposability is high
  AND coupling is low — exactly when Coase predicts decomposition pays off

## The honest gap

Charter-worker currently operates at **Level 0**: a pragmatically designed system
where some choices happen to align with organizational theory, but none were derived
from it. The architecture is fixed — the system doesn't dynamically choose between
single-agent and multi-agent modes based on measured task properties.

The roadmap to make the theory operational:

1. **Instrument** — Measure coordination costs (handoff tokens, context loss,
   error propagation) in charter-worker's own operation
2. **Decide** — Implement the Coasean boundary rule as a runtime architecture
   selector: only decompose when measured gains exceed measured costs
3. **Optimize** — Let the system learn its own optimal boundaries from operational data
4. **Validate** — Show that the theory predicts the right architecture for new tasks

The controlled experiment (arms A-D) validates the theory externally. Instrumenting
charter-worker itself would validate it internally. Both together make the strongest
possible case.
