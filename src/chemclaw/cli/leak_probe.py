"""`python -m chemclaw.cli.leak_probe` — drive real turns in one process and say what it retains.

A soak can only see the front door's RSS grow from outside; this runs the same turns inside the
measuring process, where `gc`, `tracemalloc` and the app's structures are reachable. Committed
rather than a scratch script so the measurement can be replayed.

The verdict is `chemclaw.cli.soak_report`'s `fit`/`describe`, unchanged: a slope inside its own
standard error is flat. It drives the real path — `create_app()`, middleware, per-turn graph, MCP
connectors over HTTP, the session store — with only the model faked (`cli/mock_llm`).

Usage (with `make live-up` running against the mock):
    python -m chemclaw.cli.leak_probe --turns 300 --batch 25
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import tracemalloc
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chemclaw.cli.soak_report import describe, fit
from chemclaw.core.logging import configure_logging
from chemclaw.core.markdown import render_table

logger = logging.getLogger(__name__)

# One page is 4 KiB on every platform this runs on; `statm` reports pages, not bytes.
_PAGE_KB = 4

# The turn the probe repeats: the mock's smallest behaviour (one tool call, short answer), since the
# question is retention, and a heavier turn only raises the noise floor.
_MESSAGE = "[[a-cheap]] what is the pKa of acetic acid?"


@dataclass
class Sample:
    """One batch boundary: what the process held after N turns, garbage already collected."""

    turns: int
    rss_kb: float
    gc_objects: float
    tracked_kb: float = 0.0
    top_allocations: list[str] = field(default_factory=list)
    # Live objects per type the collector sees. Unlike RSS this cannot rise from fragmentation, and
    # diffing it between batches names the leaked type directly.
    types: dict[str, int] = field(default_factory=dict)


def _type_histogram() -> dict[str, int]:
    """Live objects by type name — the diff of two of these names what is being retained."""
    counts: dict[str, int] = {}
    for obj in gc.get_objects():
        name = type(obj).__name__
        counts[name] = counts.get(name, 0) + 1
    return counts


def _positive(value: str) -> int:
    """A turn count argparse will not accept as zero or negative — the driving loop cannot end.

    A zero or negative `--batch` never advances the loop (which keeps appending histograms), and
    `--turns 0` drives nothing to fit. `--warmup` takes `_non_negative`, since zero is meaningful
    there.
    """
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative(value: str) -> int:
    """The warm-up count, where zero is a run and not a mistake — measuring from a cold process.

    `--warmup 0` deliberately fits the first turns' one-time costs (agent pool, first connector
    sessions, caches, allocator arenas). A negative warm-up would shift every turn count and shrink
    every per-turn rate.
    """
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("cannot be negative")
    return parsed


def _rss_kb() -> float:
    """This process's resident set, read the way the soak reads the front door's."""
    with open("/proc/self/statm", encoding="utf-8") as handle:
        return float(handle.read().split()[1]) * _PAGE_KB


def _drive(client: Any, turns: int) -> int:
    """Run `turns` complete turns through the front door; return how many answered."""
    answered = 0
    for _ in range(turns):
        created = client.post("/sessions", json={})
        if created.status_code != 200:
            continue
        session_id = created.json()["session_id"]
        response = client.post(f"/sessions/{session_id}/messages", json={"message": _MESSAGE})
        if response.status_code == 200:
            answered += 1
    return answered


def _sample(turns: int, *, trace: bool, baseline: Any) -> Sample:
    """Collect, then read every counter at once so they all describe the same moment."""
    gc.collect()
    sample = Sample(
        turns=turns,
        rss_kb=_rss_kb(),
        gc_objects=float(len(gc.get_objects())),
        types=_type_histogram(),
    )
    if trace:
        snapshot = tracemalloc.take_snapshot()
        sample.tracked_kb = sum(stat.size for stat in snapshot.statistics("filename")) / 1024
        if baseline is not None:
            sample.top_allocations = [
                f"{stat.size_diff / 1024:+.0f} KB {stat.traceback.format()[-1].strip()}"
                for stat in snapshot.compare_to(baseline, "lineno")[:8]
                if stat.size_diff > 0
            ]
    return sample


def _per_turn(delta: float, span: float) -> float:
    """A rate over `span` turns, or 0.0 when no turns separate the first and last sample.

    Shared by both report columns, so a degenerate series renders what it knows instead of raising
    mid-report.
    """
    return delta / span if span else 0.0


def report(samples: Sequence[Sample]) -> str:
    """What each series did per turn, as a fit — the whole deliverable."""
    if len(samples) < 2:
        return "not enough batches to fit anything"
    turns = [float(s.turns) for s in samples]
    span = turns[-1] - turns[0]
    lines = [
        f"# Leak probe: {int(turns[-1])} turns in {len(samples)} batches",
        "",
    ]
    # `describe` fits per batch; the per-turn column is the readable number, the verdict the
    # trustworthy one.
    lines.append(
        render_table(
            ["series", "first", "last", "per turn", "verdict"],
            [
                [
                    label,
                    f"{values[0]:.0f}",
                    f"{values[-1]:.0f}",
                    f"{_per_turn(values[-1] - values[0], span):+.2f} {unit}",
                    describe(values, unit + "/batch"),
                ]
                for label, values, unit in (
                    ("RSS", [s.rss_kb for s in samples], "KB"),
                    ("gc objects", [s.gc_objects for s in samples], "objects"),
                    ("tracemalloc", [s.tracked_kb for s in samples], "KB"),
                )
                if any(values)
            ],
            align="lrrrl",
        )
    )
    grown = sorted(
        (
            (samples[-1].types.get(name, 0) - samples[0].types.get(name, 0), name)
            for name in set(samples[0].types) | set(samples[-1].types)
        ),
        reverse=True,
    )[:12]
    if any(delta > 0 for delta, _ in grown):
        lines += ["", "## Live objects gained per turn, by type", ""]
        lines.append(
            render_table(
                ["type", "per turn", "total"],
                [
                    [f"`{name}`", f"{_per_turn(delta, span):+.2f}", f"{delta:+d}"]
                    for delta, name in grown
                    if delta > 0
                ],
                align="lrr",
            )
        )
    allocations = [line for sample in samples for line in sample.top_allocations]
    if allocations:
        lines += ["", "## Largest growth since the first batch", ""]
        lines += [f"- `{line}`" for line in allocations[-8:]]
    return "\n".join(lines) + "\n"


def leaks(samples: Sequence[Sample]) -> bool:
    """Whether RSS growth is resolvable against its own noise — the probe's pass/fail.

    Read off `fit`, not the endpoints: two endpoints always differ.
    """
    return bool(fit([s.rss_kb for s in samples]).resolved)


def main(argv: list[str] | None = None) -> int:
    """Drive the turns, print the fits, and exit non-zero when RSS growth is resolvable."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=_positive, default=300, help="total turns to drive")
    parser.add_argument("--batch", type=_positive, default=25, help="turns between samples")
    parser.add_argument(
        "--warmup", type=_non_negative, default=25, help="turns before the first sample (0 = cold)"
    )
    parser.add_argument(
        "--trace", action="store_true", help="also take tracemalloc snapshots (slower)"
    )
    parser.add_argument("--report", type=Path, default=Path("tasks/live-test/leak-probe.md"))
    args = parser.parse_args(argv)

    # The configured logging path, so this probe's output is redacted and context-stamped like any
    # process's.
    configure_logging()
    # The live lane's configuration (as `infra/live/processes.sh` pins it): durable session store,
    # required connectors, dedicated note checkout. Loopback host, since
    # `_refuse_unauthenticated_exposure` refuses 0.0.0.0 with `entra_required` off.
    for key, value in (
        ("CHEMCLAW_LLM_BASE_URL", "http://127.0.0.1:8820/v1"),
        ("CHEMCLAW_LLM_MODEL", "mock"),
        ("CHEMCLAW_SERVICE_HOST", "127.0.0.1"),
        ("CHEMCLAW_ENTRA_REQUIRED", "false"),
        ("CHEMCLAW_SESSION_STORE", "postgres"),
        ("CHEMCLAW_CONNECTORS_REQUIRED", "true"),
    ):
        os.environ.setdefault(key, value)
    # URLs come from `connectors_dev.build_composite()` itself, so the probe follows any change to
    # the dev runner. Assigned onto `settings` because the singleton is already built by the time
    # the URLs exist.
    from chemclaw.cli.connectors_dev import build_composite
    from chemclaw.core.config import settings

    if not settings.connector_urls:
        settings.connector_urls = build_composite()[1]

    # Imported lazily: `create_app` reads config at import, and `--help` should not need a database.
    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app

    if args.trace:
        tracemalloc.start(25)

    app = create_app()
    samples: list[Sample] = []
    baseline = None
    with TestClient(app) as client:
        # Warm-up keeps one-time costs (agent pool, first connector sessions, caches, allocator
        # arenas) out of the fitted slope; `--warmup 0` opts in to measuring them.
        answered = _drive(client, args.warmup)
        logger.info("warm-up: %d/%d turns answered", answered, args.warmup)
        gc.collect()
        if args.trace:
            baseline = tracemalloc.take_snapshot()
        samples.append(_sample(args.warmup, trace=args.trace, baseline=None))
        done = args.warmup
        while done < args.turns:
            batch = min(args.batch, args.turns - done)
            answered = _drive(client, batch)
            done += batch
            samples.append(_sample(done, trace=args.trace, baseline=baseline))
            logger.info(
                "%d turns: rss=%.0f MB, answered=%d/%d",
                done,
                samples[-1].rss_kb / 1024,
                answered,
                batch,
            )

    text = report(samples)
    print(text)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text, encoding="utf-8")
    (args.report.with_suffix(".jsonl")).write_text(
        "".join(json.dumps(vars(s), separators=(",", ":")) + "\n" for s in samples),
        encoding="utf-8",
    )
    return 1 if leaks(samples) else 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
