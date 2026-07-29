"""Progress reporting for calculations that run for minutes (xTB plan X3/X4).

A single shared vocabulary, in one small module for one reason: `calc/` must not know
about Temporal, and the modules that need to report progress (`calc.reaction`,
`calc.xtb_scan`) must not import each other to share the type. The durable activity
passes `activity.heartbeat` and gets both liveness detection and a readable progress
line; every other caller gets the default, which does nothing.

This exists because of the workload, not for tidiness. On drug-sized molecules — the
200-800 Da range these calculators are actually pointed at — a multi-species reaction or
a solvent screen runs for minutes, so a worker that dies must be noticed by a heartbeat
rather than at the job's hour-long start-to-close timeout.
"""

from collections.abc import Callable

# Called with a human-readable line each time a long run finishes a unit of work: one
# species, one solvent, one scan point.
Progress = Callable[[str], None]


def no_progress(message: str) -> None:
    """The default sink: a run nobody is watching reports to nobody."""
