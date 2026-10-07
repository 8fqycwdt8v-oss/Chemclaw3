"""When a template's steps may run at the same time — **derived** from the file, never declared.

Not a loop (that is a composite on the MCP side,
`D-2026-08-25-the-loop-is-a-composite-not-a-template`): this only asks which already-declared steps
wait on each other. `${steps.<id>.result}` is a dependency edge, and forward references are refused,
so the declared order is a topological order and needs no new syntax.

Waves rather than a general DAG scheduler: a wave is every step whose dependencies have completed,
and the next starts when the whole wave is done. A per-step scheduler would add replay-hazardous
branching to workflow code for a gain the shipped templates do not show.

Every sequence returned is in the file's declared order, because Temporal replay requires the same
decisions in the same sequence.
"""

from typing import TypeVar

from chemclaw.templates.manifest import Step, Template, step_references

#: The element of a wave. `batches` is positional, so it is generic in it.
_T = TypeVar("_T")

#: A step's dependencies, as step ids — the `${steps.<id>.result}` references it makes.
#:
#: A reference to `inputs.*` is not an edge: an input is in scope before the first step runs.
_STEP_PREFIX = "steps."


def dependencies(step: Step) -> frozenset[str]:
    """The step ids this step reads, whichever kind of step it is.

    `steps.<id>.result` and `steps.<id>.result.<field>` are the same edge.
    """
    return frozenset(
        reference[len(_STEP_PREFIX) :].split(".", 1)[0]
        for reference in step_references(step)
        if reference.startswith(_STEP_PREFIX)
    )


def schedule(template: Template) -> tuple[tuple[Step, ...], ...]:
    """`template`'s steps grouped into waves that may each run concurrently, in declared order.

    May, not will: `TemplateRunInput.max_parallel_steps` bounds how many are in flight (see
    `batches`). Wave *k* is every step whose dependencies completed in earlier waves; a chained
    template runs exactly sequentially. No cycle check is needed: forward references are refused at
    load time, so each pass places at least the first unplaced step.
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


def batches(wave: tuple[_T, ...], limit: int) -> tuple[tuple[_T, ...], ...]:
    """One wave split into runs of at most `limit` steps, in declared order; `0` means one batch.

    Shared by `TemplateWorkflow._run_wave`, which dispatches these batches, and
    `agent/template_surface.run_ceiling_problems`, which sums each batch's cost, so the two agree.
    Fixed-size batches rather than a semaphore keep dispatch deterministic under Temporal replay.
    Generic because the split is purely positional.
    """
    if limit < 1 or limit >= len(wave):
        return (wave,)
    return tuple(wave[index : index + limit] for index in range(0, len(wave), limit))


def widest(template: Template) -> int:
    """How wide this template's widest wave is.

    Not how many steps are in flight at once; that is bounded by `max_parallel_steps`.
    """
    return max((len(wave) for wave in schedule(template)), default=0)
