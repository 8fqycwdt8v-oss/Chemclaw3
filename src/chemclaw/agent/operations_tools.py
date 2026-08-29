"""The agent tool over the operational read model — one tool, four readings.

**One tool with an `aspect` rather than four tools**, which is a deliberate departure from the
one-question-one-tool shape most of this surface has. Every advertised tool's schema ships on every
turn, the static prefix is already the subject of a measured ceiling
(`tests/test_context_floor.py`), and these four readings share a window argument, a coverage field
and a single sentence of guidance. Four names would have bought nothing the enum does not and cost
four schemas.

**It reads and it does not remember.** Nothing here proposes a note, records an observation or
writes a preference: an operational reading is a projection of rows this system already wrote, so
it is `read_only` in the sense the manifest gate means and needs no gate of its own.
"""

from typing import Literal

from chemclaw.core.tool_registry import tool
from chemclaw.operations import Window, authorship, job_activity, spend, tool_usage

#: What `review_activity` can be asked for. A closed set, because the model picks it.
Aspect = Literal["tools", "jobs", "authorship", "spend"]


@tool
async def review_activity(
    aspect: Aspect = "tools",
    days: int = 30,
    tool_name: str = "",
    compare_with_previous: bool = False,
) -> dict[str, object]:
    """Read what this system itself has done — tool use, durable jobs, proposals, or effort.

    The record of the *work*, not of the chemistry: who used which capability, which jobs ran, what
    this system proposed for the knowledge graph and what people decided, and how many turns and
    tokens each actor spent. For "is anyone actually using this", "how did our hazard screening
    trend against last quarter", or "how much of what is in the graph did this system propose".

    Three limits to carry into any answer built on it:

    - **It is not project, programme, capacity or headcount data.** It knows what was asked of
      *this system*. Never present a tool-call count as a count of experiments, people or hours.
    - **It is not a share of anybody's authorship.** The `authorship` aspect returns a `boundary`
      sentence; report that sentence rather than converting the numbers into a percentage.
    - **An empty result is not automatically "nothing happened".** Every answer carries `coverage`
      with the window it ran over; if it is empty, say over what span.

    Every figure is a count, a duration, a timestamp or an identifier from a bounded vocabulary —
    nothing a caller typed. For the reasons behind a run, use `find_past_jobs`.

    Args:
        aspect: `tools` — calls per tool split by outcome (ok, refused, error, cancelled). `jobs` —
            durable runs per connector job, and how many proposed a note. `authorship` — what this
            system proposed for the graph, by note type, and how humans decided. `spend` — turns,
            tokens and wall clock per actor.
        days: How far back to look, ending now. Clamped to 1..730; quote `coverage`, not this.
        tool_name: With `aspect="tools"`, narrow to one tool name. Ignored by the other aspects.
        compare_with_previous: Also return the same reading over the preceding window of equal
            length, as `previous`. A quarter-on-quarter question needs this; without it there is
            one span and no trend to state.

    Returns:
        The reading under `current`, each carrying its own `coverage`; plus `previous` when a
        comparison was asked for.
    """
    window = Window.trailing(days)

    async def read(span: Window) -> dict[str, object]:
        if aspect == "jobs":
            return (await job_activity(span)).model_dump()
        if aspect == "authorship":
            return (await authorship(span)).model_dump()
        if aspect == "spend":
            return (await spend(span)).model_dump()
        return (await tool_usage(span, tool=tool_name or None)).model_dump()

    result: dict[str, object] = {"aspect": aspect, "current": await read(window)}
    if compare_with_previous:
        result["previous"] = await read(window.preceding())
    return result
