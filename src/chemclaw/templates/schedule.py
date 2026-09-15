"""When a template's steps may run at the same time — **derived** from the file, never declared.

**This is not the fan-out `D-2026-08-25-the-loop-is-a-composite-not-a-template` declined**, and the
distinction is the whole reason this module can exist without reopening that decision. That ADR is
about a *loop*: ranking N microstates or surveying N bonds, where N is known only once an earlier
step has answered. A loop needs iteration and expressions, which is how a config format becomes a
programming language with no debugger — so the loop lives in a composite on the MCP side and the
sequence lives in the template.

What this module asks is a different question with a different answer: of the steps a file has
*already declared*, which of them wait on each other? That is not a new capability and needs no new
syntax, because **the template already states it**. `${steps.<id>.result}` is a dependency edge, and
`Template._references_resolve_and_point_backwards` refuses a forward reference — so the declared
order is a topological order of a DAG, and every edge in that DAG is written down. Two steps with no
path between them were always independent; running them one after the other was a property of the
sequencer, not of the procedure.

**Waves rather than a general DAG scheduler**, which is a deliberate simplification and not a
limitation anybody will hit soon. A wave is the set of steps whose dependencies have all completed,
run together; the next wave starts when the whole wave is done. A true scheduler would start a step
the instant *its own* dependencies finish, which is better when one long step shares a wave with a
short one — and costs a per-step completion graph in workflow code, where every extra branch is a
replay hazard. The shipped catalogue is two and three steps; when a template exists that the
difference is measurable on, the bound below is what to re-derive.

**The order inside a wave is the declared order, everywhere and on purpose.** Temporal replays
workflow code and expects the same decisions in the same sequence, so anything derived from set
iteration, dict ordering or completion timing is a divergence waiting for a worker restart. Every
sequence this module returns is ordered by the step's position in the file.
"""

from chemclaw.templates.manifest import Step, Template, step_references

#: A step's dependencies, as step ids — the `${steps.<id>.result}` references it makes.
#:
#: A reference to `inputs.*` is not an edge: an input is in scope before the first step runs.
_STEP_PREFIX = "steps."


def dependencies(step: Step) -> frozenset[str]:
    """The step ids this step reads, whichever kind of step it is.

    Reads `steps.<id>.result` and `steps.<id>.result.<field>` alike — the edge is to the step, and
    which part of its result is being read changes nothing about *when* it may run.

    Args:
        step: The step to read references off.

    Returns:
        The ids this step waits for, empty when it waits for nothing.
    """
    return frozenset(
        reference[len(_STEP_PREFIX) :].split(".", 1)[0]
        for reference in step_references(step)
        if reference.startswith(_STEP_PREFIX)
    )


def schedule(template: Template) -> tuple[tuple[Step, ...], ...]:
    """`template`'s steps grouped into waves that may each run concurrently.

    Wave *k* is every step all of whose dependencies completed in waves before *k*. A template
    whose steps chain — which is seven of the nine shipped ones — yields one step per wave and runs
    exactly as it did before this module existed, which is the property that makes this safe to
    apply unconditionally rather than behind a flag nobody sets.

    **Terminates without a cycle check, because a cycle cannot be built.**
    `Template._references_resolve_and_point_backwards` refuses a reference to a step that has not
    run yet, so every edge points at an earlier position in the list and each pass places at least
    the first unplaced step. A cycle would have to have been refused at load time; asserting it
    again here would be a guard whose only caller is its own test.

    Args:
        template: The template to schedule. Its steps are already a topological order.

    Returns:
        The waves, in order; each wave in the file's own step order.
    """
    waves: list[tuple[Step, ...]] = []
    placed: set[str] = set()
    remaining = list(template.steps)
    while remaining:
        wave = tuple(step for step in remaining if dependencies(step) <= placed)
        waves.append(wave)
        placed.update(step.id for step in wave)
        remaining = [step for step in remaining if step.id not in placed]
    return tuple(waves)


def widest(template: Template) -> int:
    """How many steps this template ever has in flight at once.

    Read by the tests that assert a chained template did not silently gain concurrency, and by the
    ceiling arithmetic's own explanation of why a sum over waves is not a sum over steps.
    """
    return max((len(wave) for wave in schedule(template)), default=0)
