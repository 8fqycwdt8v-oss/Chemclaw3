"""`python -m chemclaw.cli.live_probes` — run the live probe set against a running front door.

The terminal entrypoint for the AG-13 behaviour eval. Everything it prints is derived from
transcripts already written to disk, so the summary is a view of the evidence rather than a
separate claim about it.

The report deliberately separates *coverage* from *quality*. A run that answers every probe while
calling no tools is not a good run — it is a system whose capability the model never reached, and
the fifty-question pass this inherits from found exactly that (sixteen of fifty answers used no
tool at all, nine of them on questions the surface covered). So the tool-reach number is printed
beside the verdict counts, never folded into them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.evals.live import ProbeOutcome, load_probes, run_probes
from chemclaw.evals.live_judge import Judgement, judge_outcome, judgement_from_transcript
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)


def _summary(probes: list[Probe], outcomes: list[ProbeOutcome], grades: list[Judgement]) -> str:
    """The run in one table per axis: verdicts, tool reach, failure visibility, per section."""
    by_id = {p.id: p for p in probes}
    verdicts = Counter(g.verdict for g in grades)
    lines: list[str] = []

    lines.append(f"# Live probe run — {len(outcomes)} probes\n")
    lines.append("## Verdicts\n")
    lines.append("| verdict | count | share |")
    lines.append("| --- | ---: | ---: |")
    for verdict in ("served", "partial", "unserved", "fabricated", "ungraded"):
        count = verdicts.get(verdict, 0)
        lines.append(f"| {verdict} | {count} | {count / max(len(grades), 1):.0%} |")

    answered = sum(1 for o in outcomes if o.answered)
    zero_tool = [o for o in outcomes if not o.tools_called]
    zero_tool_covered = [o for o in zero_tool if by_id[o.probe_id].bucket == "A"]
    expected = [o for o in outcomes if o.expected_tools_met is not None]
    reached = [o for o in expected if o.expected_tools_met]
    silent = [o for o in outcomes if not o.answered and not o.failed_loudly]
    uncited = [o for o in outcomes if o.uncited_note_ids]

    lines.append("\n## Coverage and honesty\n")
    lines.append("| signal | value |")
    lines.append("| --- | ---: |")
    lines.append(f"| answered at all | {answered} / {len(outcomes)} |")
    lines.append(f"| expected tool reached | {len(reached)} / {len(expected)} |")
    lines.append(f"| answers using no tool at all | {len(zero_tool)} / {len(outcomes)} |")
    lines.append(
        f"| …of those, on questions the surface covers (bucket A) | {len(zero_tool_covered)} |"
    )
    lines.append(f"| **failed silently** (no answer, no error) | **{len(silent)}** |")
    lines.append(f"| **answers citing a note no tool returned** | **{len(uncited)}** |")
    lines.append(
        f"| clarified via ask_clarifying_question | "
        f"{sum(1 for o in outcomes if o.asked_clarifying)} |"
    )
    lines.append(
        f"| …and clarified in prose instead (the tool existed) | "
        f"{sum(1 for o in outcomes if o.asked_clarifying_in_prose)} |"
    )
    lines.append(
        f"| turns that surfaced a failure | {sum(1 for o in outcomes if o.failed_loudly)} |"
    )
    lines.append(f"| durable jobs started | {sum(len(o.jobs_started) for o in outcomes)} |")

    latencies = sorted(o.latency_seconds for o in outcomes)
    if latencies:
        lines.append(f"| median turn | {latencies[len(latencies) // 2]:.1f} s |")

    lines.append("\n## By bucket\n")
    lines.append("| bucket | probes | served | partial | unserved | fabricated | ungraded |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    grade_by_id = {g.probe_id: g for g in grades}
    for bucket in ("A", "B", "C"):
        ids = [p.id for p in probes if p.bucket == bucket]
        counts = Counter(grade_by_id[i].verdict for i in ids if i in grade_by_id)
        lines.append(
            f"| {bucket} | {len(ids)} | {counts.get('served', 0)} | {counts.get('partial', 0)} "
            f"| {counts.get('unserved', 0)} | {counts.get('fabricated', 0)} "
            f"| {counts.get('ungraded', 0)} |"
        )

    fabricated = [g for g in grades if g.verdict == "fabricated"]
    if fabricated:
        lines.append("\n## Fabrications (highest severity)\n")
        for grade in fabricated:
            probe = by_id[grade.probe_id]
            lines.append(
                f"- **{grade.probe_id}** (§{probe.section}, bucket {probe.bucket}): {grade.reason}"
            )
            for claim in grade.fabricated_claims:
                lines.append(f"  - {claim!r}")

    if silent:
        lines.append("\n## Silent failures\n")
        for outcome in silent:
            why = outcome.transport_error or "no answer, no error event"
            lines.append(f"- **{outcome.probe_id}**: {why}")

    return "\n".join(lines) + "\n"


def _load_transcripts(directory: Path) -> tuple[list[Probe], list[ProbeOutcome]]:
    """Every stored transcript in `directory`, as the probe/outcome pair that produced it."""
    probes: list[Probe] = []
    outcomes: list[ProbeOutcome] = []
    for path in sorted(directory.glob("*.json")):
        probe, outcome = judgement_from_transcript(json.loads(path.read_text(encoding="utf-8")))
        probes.append(probe)
        outcomes.append(outcome)
    return probes, outcomes


def _write_outputs(transcript_dir: Path, report: str, grades: list[Judgement]) -> None:
    """Write the summary and grades *beside their own transcripts*, never in a shared parent.

    Two bugs in one line, both of which destroyed evidence. The outputs were written to
    `transcript_dir.parent`, so a second run against a different transcript directory silently
    overwrote the first run's results — and `grades.json` was written unconditionally, so a
    `--no-judge` run replaced 190 real verdicts with `[]`. That happened, and the file was only
    recoverable because it had been committed.

    So: outputs live with the transcripts that produced them, and a run that graded nothing writes
    no grades file. An empty grades file is indistinguishable from a run where every answer failed.
    """
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "summary.md").write_text(report, encoding="utf-8")
    if grades:
        (transcript_dir / "grades.json").write_text(
            json.dumps([g.model_dump() for g in grades], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


async def _main(args: argparse.Namespace) -> int:
    if args.regrade:
        # Re-grade without re-asking. The first run's verdicts were wrong for a reason that had
        # nothing to do with the system under test — a grader token ceiling — and re-running 190
        # live questions to fix a grader bug would have changed the subject as well as the
        # measurement.
        directory = Path(args.transcript_dir or settings.live_probe_transcript_dir)
        probes, outcomes = _load_transcripts(directory)
        logger.info("re-grading %d stored transcripts from %s", len(outcomes), directory)
        by_id = {p.id: p for p in probes}
        semaphore = asyncio.Semaphore(settings.live_probe_concurrency)

        async def regrade(outcome: ProbeOutcome) -> Judgement:
            async with semaphore:
                return await judge_outcome(by_id[outcome.probe_id], outcome)

        regraded: list[Judgement] = list(await asyncio.gather(*(regrade(o) for o in outcomes)))
        report = _summary(probes, outcomes, regraded)
        print(report)
        _write_outputs(directory, report, regraded)
        return 0

    probes = load_probes(args.probe_dir)
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted or str(p.section) in wanted]
    if args.limit:
        probes = probes[: args.limit]
    logger.info(
        "running %d probes against %s", len(probes), args.base_url or settings.live_probe_base_url
    )

    outcomes = await run_probes(probes, base_url=args.base_url, transcript_dir=args.transcript_dir)

    grades: list[Judgement] = []
    if not args.no_judge:
        by_id = {p.id: p for p in probes}
        semaphore = asyncio.Semaphore(settings.live_probe_concurrency)

        async def grade(outcome: ProbeOutcome) -> Judgement:
            async with semaphore:
                return await judge_outcome(by_id[outcome.probe_id], outcome)

        grades = list(await asyncio.gather(*(grade(o) for o in outcomes)))

    report = _summary(probes, outcomes, grades)
    print(report)

    _write_outputs(Path(args.transcript_dir or settings.live_probe_transcript_dir), report, grades)
    return 0


def main() -> int:
    """Parse arguments and run the probe set."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Run the live probe set against a running front door."
    )
    parser.add_argument("--probe-dir", default=None, help="override the configured probe directory")
    parser.add_argument("--base-url", default=None, help="front door base URL")
    parser.add_argument("--transcript-dir", default=None, help="where transcripts are written")
    parser.add_argument("--only", default=None, help="comma-separated probe ids or section numbers")
    parser.add_argument("--limit", type=int, default=0, help="run at most N probes")
    parser.add_argument(
        "--no-judge", action="store_true", help="skip grading (mechanical signals only)"
    )
    parser.add_argument(
        "--regrade",
        action="store_true",
        help="re-grade stored transcripts without re-running any probe",
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
