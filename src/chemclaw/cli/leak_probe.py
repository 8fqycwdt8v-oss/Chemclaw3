"""`python -m chemclaw.cli.leak_probe` — drive real turns in one process and say what it retains.

The soak measured the *symptom* and could not name the cause: the front door's RSS grows without
bound under sustained load — 549 MB → 1,066 MB over 162 rounds, steepening, ~47 KB per turn — and a
soak samples a process from the outside, so it can only ever report that. This runs the same turns
*inside* the measuring process, where `gc`, `tracemalloc` and the app's own structures are all
reachable.

**It exists as a committed CLI rather than a scratch script for the reason `live_storm` does.** The
2026-07 load test's harness was out-of-tree and no longer exists, so every number in its record is a
rebuild rather than a replay. A leak hunt produces exactly one durable artefact — the measurement
that says whether the leak is still there — and that artefact is worthless if the thing that
produced it is gone.

**The verdict is `chemclaw.cli.soak_report`'s, not a second opinion.** Retained bytes per turn is a
trend like any other, and this module would otherwise grow its own quietly-different idea of what
counts as growth — which is the failure `D-2026-08-05-a-trend-needs-a-tail` records. So the samples
go through `fit`/`describe` unchanged: a slope inside its own standard error is reported as flat,
and the two halves are compared to each other rather than to the whole.

What it drives is the *real* path: `create_app()`, the real middleware stack, the real agent pool,
real MCP connectors over HTTP, and the real session store — against `cli/mock_llm`, so the only
faked thing is the model. That matters because an earlier in-process repro that faked the agent and
the connectors found **zero** retention across 900 turns, which is what narrowed the search to the
three things it had replaced.

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

logger = logging.getLogger(__name__)

# One page is 4 KiB on every platform this runs on; `statm` reports pages, not bytes.
_PAGE_KB = 4

# The turn the probe repeats. `a-cheap` is the mock catalogue's smallest behaviour — one tool call
# and a short answer — because the question is what a turn *retains*, not what it costs. A heavier
# behaviour would raise the noise floor without changing the slope being measured.
_MESSAGE = "[[a-cheap]] what is the pKa of acetic acid?"


@dataclass
class Sample:
    """One batch boundary: what the process held after N turns, garbage already collected."""

    turns: int
    rss_kb: float
    gc_objects: float
    # `len(agent._async_exit_stack._exit_callbacks)` summed over every agent the pool has built.
    # MAF re-enters a run-scoped MCP tool that is not connected into the *agent's* own
    # process-lifetime stack, and `AgentPool` never uses an agent as a context manager, so that
    # stack is never unwound. If this number climbs with turns, that is the leak.
    #
    # It did not: measured flat at 0 across 200 turns, which killed the hypothesis and sent the
    # search to the type histogram below — where the answer was.
    agent_exit_callbacks: float = 0.0
    tracked_kb: float = 0.0
    top_allocations: list[str] = field(default_factory=list)
    # How many live objects of each type the collector can see. The decisive series: RSS can rise
    # from allocator fragmentation, and a *tracked object count* cannot — an object is either
    # reachable or it is not. Diffing this between batches names the leaked type directly, which
    # `tracemalloc` only does by allocation site.
    types: dict[str, int] = field(default_factory=dict)


def _type_histogram() -> dict[str, int]:
    """Live objects by type name — the diff of two of these names what is being retained."""
    counts: dict[str, int] = {}
    for obj in gc.get_objects():
        name = type(obj).__name__
        counts[name] = counts.get(name, 0) + 1
    return counts


def _rss_kb() -> float:
    """This process's resident set, read the way the soak reads the front door's."""
    with open("/proc/self/statm", encoding="utf-8") as handle:
        return float(handle.read().split()[1]) * _PAGE_KB


def _pool_exit_callbacks(app: Any) -> float:
    """How many callbacks the pooled agents' process-lifetime exit stacks are holding.

    Duck-typed all the way down, deliberately: these are MAF internals, they are not a stable
    surface, and a probe that raises because a private attribute moved is worse than one that
    reports zero and lets the other signals decide.
    """
    pool = getattr(getattr(app, "state", None), "agent_pool", None)
    queues = getattr(pool, "_free", None)
    if not isinstance(queues, dict):
        return 0.0
    total = 0
    for queue in queues.values():
        for agent in list(getattr(queue, "_queue", []) or []):
            stack = getattr(agent, "_async_exit_stack", None)
            total += len(getattr(stack, "_exit_callbacks", ()) or ())
    return float(total)


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


def _sample(app: Any, turns: int, *, trace: bool, baseline: Any) -> Sample:
    """Collect, then read every counter at once so they all describe the same moment."""
    gc.collect()
    sample = Sample(
        turns=turns,
        rss_kb=_rss_kb(),
        gc_objects=float(len(gc.get_objects())),
        agent_exit_callbacks=_pool_exit_callbacks(app),
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


def report(samples: Sequence[Sample]) -> str:
    """What each series did per turn, as a fit — the whole deliverable."""
    if len(samples) < 2:
        return "not enough batches to fit anything"
    turns = [float(s.turns) for s in samples]
    span = turns[-1] - turns[0]
    lines = [
        f"# Leak probe: {int(turns[-1])} turns in {len(samples)} batches",
        "",
        "| series | first | last | per turn | verdict |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for label, values, unit in (
        ("RSS", [s.rss_kb for s in samples], "KB"),
        ("gc objects", [s.gc_objects for s in samples], "objects"),
        ("agent exit callbacks", [s.agent_exit_callbacks for s in samples], "callbacks"),
        ("tracemalloc", [s.tracked_kb for s in samples], "KB"),
    ):
        if not any(values):
            continue
        per_turn = (values[-1] - values[0]) / span if span else 0.0
        # `describe` fits against the *batch index*, so its slope is per batch; the per-turn column
        # beside it is the number a reader wants and the verdict is the number they can trust.
        lines.append(
            f"| {label} | {values[0]:.0f} | {values[-1]:.0f} | {per_turn:+.2f} {unit} | "
            f"{describe(values, unit + '/batch')} |"
        )
    grown = sorted(
        (
            (samples[-1].types.get(name, 0) - samples[0].types.get(name, 0), name)
            for name in set(samples[0].types) | set(samples[-1].types)
        ),
        reverse=True,
    )[:12]
    if any(delta > 0 for delta, _ in grown):
        lines += ["", "## Live objects gained per turn, by type", "", "| type | per turn | total |"]
        lines.append("| --- | ---: | ---: |")
        for delta, name in grown:
            if delta <= 0:
                continue
            lines.append(f"| `{name}` | {delta / span:+.2f} | {delta:+d} |")
    allocations = [line for sample in samples for line in sample.top_allocations]
    if allocations:
        lines += ["", "## Largest growth since the first batch", ""]
        lines += [f"- `{line}`" for line in allocations[-8:]]
    return "\n".join(lines) + "\n"


def leaks(samples: Sequence[Sample]) -> bool:
    """Whether RSS growth is resolvable against its own noise — the probe's pass/fail.

    Read off `fit` rather than off the endpoints, for the reason the soak's own record had to be
    rewritten twice: two endpoints will always differ, and the difference is not evidence.
    """
    return bool(fit([s.rss_kb for s in samples]).resolved)


def main(argv: list[str] | None = None) -> int:
    """Drive the turns, print the fits, and exit non-zero when RSS growth is resolvable."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=300, help="total turns to drive")
    parser.add_argument("--batch", type=int, default=25, help="turns between samples")
    parser.add_argument("--warmup", type=int, default=25, help="turns before the first sample")
    parser.add_argument(
        "--trace", action="store_true", help="also take tracemalloc snapshots (slower)"
    )
    parser.add_argument("--report", type=Path, default=Path("tasks/live-test/leak-probe.md"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # The lane's configuration, not a lighter one. LIVE-8's lesson is that a configuration only
    # production sets is a configuration nothing tests, and this probe's whole claim is that it
    # drives the same path the soak measured — so it pins what `infra/live/processes.sh` pins:
    # the durable session store, required connectors, and the dedicated note checkout. The
    # loopback host is not cosmetic either: `_refuse_unauthenticated_exposure` correctly refuses
    # to build an app that binds 0.0.0.0 with `entra_required` off.
    for key, value in (
        ("CHEMCLAW_LLM_PROVIDER", "openai_compatible"),
        ("CHEMCLAW_LLM_BASE_URL", "http://127.0.0.1:8820/v1"),
        ("CHEMCLAW_LLM_MODEL", "mock"),
        ("CHEMCLAW_SERVICE_HOST", "127.0.0.1"),
        ("CHEMCLAW_ENTRA_REQUIRED", "false"),
        ("CHEMCLAW_SESSION_STORE", "postgres"),
        ("CHEMCLAW_CONNECTORS_REQUIRED", "true"),
    ):
        os.environ.setdefault(key, value)
    # The connector URLs come from `connectors_dev.build_composite()` itself rather than being
    # rebuilt here from the same string pattern — the rule `infra/live/processes.sh` states and
    # follows. One reader for one shape: if the dev runner changes its port or its mount path, this
    # follows automatically instead of drifting into a probe that measures a differently-configured
    # process than the lane does.
    #
    # Assigned onto `settings` rather than into the environment, because `settings` is a singleton
    # built on first import and `build_composite` imports it — so by the time the URLs exist, an
    # environment variable is already too late to be read.
    from chemclaw.cli.connectors_dev import build_composite
    from chemclaw.core.config import settings

    if not settings.connector_urls:
        settings.connector_urls = build_composite()[1]

    # Imported here, not at module scope: `create_app` pulls in the whole service and reads config
    # at import time, and this module's `--help` should not need a database to answer.
    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app

    if args.trace:
        tracemalloc.start(25)

    app = create_app()
    samples: list[Sample] = []
    baseline = None
    with TestClient(app) as client:
        # The warm-up is not cosmetic. The first turns build the agent pool, open every connector's
        # first session, fill the JWKS and profile caches and let the allocator find its arenas —
        # all one-time costs that would otherwise be fitted as a slope.
        answered = _drive(client, args.warmup)
        logger.info("warm-up: %d/%d turns answered", answered, args.warmup)
        gc.collect()
        if args.trace:
            baseline = tracemalloc.take_snapshot()
        samples.append(_sample(app, args.warmup, trace=args.trace, baseline=None))
        done = args.warmup
        while done < args.turns:
            batch = min(args.batch, args.turns - done)
            answered = _drive(client, batch)
            done += batch
            samples.append(_sample(app, done, trace=args.trace, baseline=baseline))
            logger.info(
                "%d turns: rss=%.0f MB, exit-callbacks=%.0f, answered=%d/%d",
                done,
                samples[-1].rss_kb / 1024,
                samples[-1].agent_exit_callbacks,
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
