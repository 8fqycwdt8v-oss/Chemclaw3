"""`python -m chemclaw.cli.retrieval_arms` — score retrieval configurations against the gold set.

The instrument behind `BACKLOG.md`'s correlation row. That row has now accumulated four measured
no-ops, each found by somebody building this measurement from scratch and each recorded as prose
afterwards — so the next person reaches for the prose, finds a number attached to a commit that has
moved, and rebuilds the harness to check it. This module is that harness, kept.

**What it measures.** Every probe in `data/evals/probes/` that declares `expects_notes` is asked of
the real `gather_evidence`, and each labelled note's position in the merged list is recorded. The
outputs are the figures the row is argued in: how many gold notes were found at all, their mean and
median rank, how many landed in the top 3 and top 5, and — against a named baseline arm — how many
moved up and how many moved down.

**Why rank rather than recall alone.** Both, and the distinction decides things: `retrieval_recall`
is the gated metric (`evals/retrieval.py`), so a configuration that finds fewer gold notes is worse
however well it ranks the ones it finds. Measured on 2026-09-15, dropping the dense leg took mean
gold rank from 4.69 to 3.69 — the first configuration ever measured to beat the shipped default —
and lost 3 of 39 gold notes, one of them at rank 3. Printing only the rank would have made that read
as a win.

**It needs Postgres and a built note index**, because an arm naming `vector` reindexes the shipped
corpus before it asks anything. That is the whole reason this measurement kept being deferred; the
sandbox runs Postgres (`make up`), so it is not a reason any more.

Each arm runs in a **subprocess**, not in a loop here: `CHEMCLAW_DATA_SOURCES` is read when
`Settings()` is constructed and the capability-tool registry refuses a second registration, so
reloading the modules in-process raises rather than re-reading the environment. A subprocess per arm
is the honest way to vary a setting that is read at import.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import yaml

from chemclaw.core.config import settings
from chemclaw.evals.probe import Probe, ProbeSet

#: The arms this reproduces by default: the shipped merge, the fusion, and the two remedies that
#: `BACKLOG.md` argues about. Named rather than generated, because each one is a claim somebody made
#: and the point of the default set is that running it re-checks every one of them at once.
DEFAULT_ARMS: tuple[tuple[str, str, str, str], ...] = (
    ("round-robin, 3 legs (shipped)", "graph,lexical,vector", "graph", ""),
    ("RRF, 3 legs", "graph,lexical,vector", "hybrid", ""),
    ("RRF, 3 legs, vector at 0.5", "graph,lexical,vector", "hybrid", '{"vector": 0.5}'),
    ("RRF, 3 legs, vector at 0.1", "graph,lexical,vector", "hybrid", '{"vector": 0.1}'),
    ("RRF, 2 legs (graph+lexical)", "graph,lexical", "hybrid", ""),
    ("RRF, 2 legs (graph+vector)", "graph,vector", "hybrid", ""),
)


def probes_with_labels() -> list[Probe]:
    """Every probe declaring `expects_notes`, parsed through `ProbeSet` rather than as loose dicts.

    Validated on the way in for the reason `tests/test_probe_coverage.py` gives: a malformed probe
    would otherwise sit in the corpus being counted and never asked.
    """
    found: list[Probe] = []
    for path in sorted(Path(settings.live_probe_dir).rglob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found.extend(ProbeSet.model_validate(document).probes)
    return [probe for probe in found if probe.expects_notes]


async def _measure_this_arm() -> dict[str, int | None]:
    """Ask every labelled probe and record where each expected note landed. Runs in the child.

    A note the merged list does not contain is `None` rather than a large rank: it was not found,
    which is a different failure from being found late, and averaging a sentinel would hide it in
    the mean.
    """
    from chemclaw.agent.research_tools import gather_evidence

    if "vector" in os.environ.get("CHEMCLAW_DATA_SOURCES", ""):
        from chemclaw.retrieval.vector_index import default_note_index, reindex_notes

        await reindex_notes(default_note_index(), full=True)

    ranks: dict[str, int | None] = {}
    for probe in probes_with_labels():
        sweep = await gather_evidence(probe.question)
        chunks = getattr(sweep, "sweep", sweep).chunks
        order = list(dict.fromkeys(chunk.source_note_id for chunk in chunks))
        for note in probe.expects_notes:
            ranks[f"{probe.id}|{note}"] = order.index(note) + 1 if note in order else None
    return ranks


def run_arm(sources: str, mode: str, weights: str) -> dict[str, int | None]:
    """Run one arm in a subprocess and return its rank map.

    Raises:
        RuntimeError: the child produced no result line — its stderr is included, because a silent
            empty arm would otherwise be averaged as though it had answered.
    """
    environment = dict(os.environ, CHEMCLAW_DATA_SOURCES=sources, CHEMCLAW_RETRIEVAL_MODE=mode)
    if weights:
        environment["CHEMCLAW_RETRIEVAL_SOURCE_WEIGHTS"] = weights
    completed = subprocess.run(
        [sys.executable, "-m", "chemclaw.cli.retrieval_arms", "--child"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT"):
            parsed: dict[str, int | None] = json.loads(line[len("RESULT") :])
            return parsed
    raise RuntimeError(f"arm {sources!r}/{mode!r} produced no result:\n{completed.stderr[-2000:]}")


def _summarise(label: str, ranks: dict[str, int | None], baseline: dict[str, int | None]) -> str:
    """One table row: how much was found, where it landed, and how it moved against the baseline."""
    found = [rank for rank in ranks.values() if rank is not None]

    def moved(direction: int) -> int:
        """Pairs this arm ranks `direction` (-1 better, +1 worse) than the baseline does.

        Only pairs both arms found are counted: a note one arm misses entirely is a recall change,
        which `lost` reports separately, and folding it in here would let a configuration that
        stops finding a note read as having improved its rank.
        """
        count = 0
        for key, rank in ranks.items():
            was = baseline.get(key)
            if (
                rank is not None
                and was is not None
                and (rank > was) == (direction > 0)
                and rank != was
            ):
                count += 1
        return count

    better, worse = moved(-1), moved(1)
    lost = sum(1 for key, rank in ranks.items() if rank is None and baseline.get(key) is not None)
    return (
        f"{label:34s} {len(found):3d}/{len(ranks):<3d} {statistics.mean(found):6.2f} "
        f"{statistics.median(found):6.1f} {sum(1 for r in found if r <= 3):5d} "
        f"{sum(1 for r in found if r <= 5):5d} {better:5d} {worse:5d} {lost:5d}"
    )


def main() -> int:
    """Run every arm and print the comparison table. The first arm is the baseline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    if arguments.child:
        print("RESULT" + json.dumps(asyncio.run(_measure_this_arm())))
        return 0

    labelled = probes_with_labels()
    pairs = sum(len(probe.expects_notes) for probe in labelled)
    print(f"{len(labelled)} probes, {pairs} labelled (query, note) pairs\n")

    results: list[tuple[str, dict[str, int | None]]] = []
    for label, sources, mode, weights in DEFAULT_ARMS:
        print(f"running {label} ...", file=sys.stderr)
        results.append((label, run_arm(sources, mode, weights)))

    baseline = results[0][1]
    header = (
        f"{'arm':34s} {'found':>7s} {'mean':>6s} {'median':>6s} "
        f"{'top3':>5s} {'top5':>5s} {'up':>5s} {'down':>5s} {'lost':>5s}"
    )
    print(header)
    print("-" * len(header))
    for label, ranks in results:
        print(_summarise(label, ranks, baseline))
    print(
        "\n`up`/`down`/`lost` are against the first arm. `lost` is a gold note this arm does not "
        "find at all,\nwhich `retrieval_recall` gates on — a configuration that ranks better and "
        "loses notes is not better."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
