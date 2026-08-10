"""`python -m chemclaw.cli.live_probes` — run the live probe set against a running front door.

The terminal entrypoint for the AG-13 behaviour eval. Everything it prints is derived from
transcripts already written to disk, so the summary is a view of the evidence rather than a
separate claim about it.

The report deliberately separates *coverage* from *quality*. A run that answers every probe while
calling no tools is not a good run — it is a system whose capability the model never reached, and
the fifty-question pass this inherits from found exactly that (sixteen of fifty answers used no
tool at all, nine of them on questions the surface covered). So the tool-reach number is printed
beside the verdict counts, never folded into them.

**`--suite` selects what is being asked, and the default asks today's corpus question.** Three M12
suites live beside it (`--suite plan-gate|degradation|routing`), each running its own probe file
from `settings.live_m12_probe_dir` and each scored mechanically rather than by a judge. They are
flags on this entry point rather than three new modules because everything underneath is shared —
the front-door client, the transcript discipline, the "outputs land beside their own transcripts"
rule — and a second copy of that is exactly how two harnesses come to disagree about what a turn
did. Every one of them exits non-zero on a failed check *or* on a check it could not take, because
a measurement that did not happen is not a measurement that passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

import httpx
import yaml

from chemclaw.connectors.registry import job_names
from chemclaw.core.config import settings
from chemclaw.evals.live import (
    Finding,
    PlanGateRun,
    ProbeOutcome,
    RoutingScore,
    degradation_findings,
    load_probes,
    run_plan_gate_probe,
    run_probes,
    run_turn,
    score_routing,
    session_tokens,
)
from chemclaw.evals.live_judge import Judgement, judge_outcome, judgement_from_transcript
from chemclaw.evals.probe import Probe, ProbeSet

logger = logging.getLogger(__name__)

# The M12 suites, and the probe file each one runs. Declared as a map rather than derived from the
# suite name so that a suite whose file is missing fails at the lookup with a name a reader can
# search for, instead of raising `FileNotFoundError` on a path nobody wrote down.
_M12_SUITES: dict[str, str] = {
    "plan-gate": "plan_gate.yaml",
    "degradation": "degradation.yaml",
    "routing": "routing.yaml",
}


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

    # What the broker says became of those jobs, for the probes that declared they needed one.
    # Reported beside the launch count and never folded into it, for the same reason tool reach is
    # kept beside the verdicts: "started" and "ran" are different facts, and a run that collapsed
    # them would report a durable system it had not observed. `RUNNING` is not a failure — a
    # campaign outlives its turn by design — so the states are listed rather than scored.
    job_states = Counter(state for outcome in outcomes for state in outcome.job_outcomes.values())
    if job_states:
        summary = " · ".join(f"{state} {count}" for state, count in sorted(job_states.items()))
        lines.append(f"| …and what Temporal says became of them | {summary} |")

    # Whether an `expects_job` probe reached the durable path at all — asked of the *tool calls*,
    # not of the `job_started` events.
    #
    # This distinction is the first thing the signal got wrong, on its first live run. A job that
    # answers inside `inline_wait_seconds` is deliberately never announced (`connectors/jobs.py`:
    # an already-finished run would never emit the matching `job_completed`, so the surface would
    # draw a row that stays "running" forever), so du-01 ran `compute_reaction_energy` end to end
    # through Temporal — workflow `calc-compute_reaction_energy-4cf212292f8f8e4e`, COMPLETED — and
    # was reported as having started no job. A signal that calls a *working* durable path a miss is
    # worse than no signal, because it spends the reader's attention on the one thing that was fine.
    if any(p.expects_job for p in probes):
        jobs = set(job_names())
        ran_a_job = {o.probe_id for o in outcomes if o.job_outcomes or (jobs & set(o.tools_called))}
        inline = {
            o.probe_id for o in outcomes if not o.job_outcomes and (jobs & set(o.tools_called))
        }
        missed = sorted({p.id for p in probes if p.expects_job} - ran_a_job)
        if inline:
            lines.append(
                f"| …of which finished inside the turn (never announced) | {len(inline)} |"
            )
        if missed:
            lines.append(
                f"| **probes needing a durable job that ran none** | **{', '.join(missed)}** |"
            )

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


def _client(base_url: str | None) -> httpx.AsyncClient:
    """A front-door client on the configured timeout — one construction, so three suites agree."""
    return httpx.AsyncClient(
        base_url=base_url if base_url is not None else settings.live_probe_base_url,
        timeout=httpx.Timeout(settings.live_probe_timeout_seconds),
    )


def _suite_dir(transcript_dir: str | None, suite: str) -> Path:
    """Where one suite's transcripts and report land.

    A subdirectory per suite by default, for the reason `_write_outputs` records at length: outputs
    live *with* the transcripts that produced them, so two suites cannot overwrite each other's
    summary the way two probe runs into one parent once did.
    """
    if transcript_dir is not None:
        return Path(transcript_dir)
    return Path(settings.live_probe_transcript_dir) / suite


def _findings_report(title: str, preamble: str, findings: list[Finding], notes: list[str]) -> str:
    """The shared shape of a suite report: what ran, what was observed, and what was not taken.

    Every row resolves to something the harness saw. A check that could not be taken is a row with
    `ok=False` and an `observed` saying why, never an absent row — the coverage lesson
    `cli/live_storm.report` was rewritten around, applied to a table one tenth the size.
    """
    lines = [f"# {title}\n", preamble, ""]
    lines.extend(f"- {note}" for note in notes)
    lines.append("")
    lines.append("| probe | check | result | observed |")
    lines.append("| --- | --- | --- | --- |")
    for finding in findings:
        verdict = "PASS" if finding.ok else "**FAIL**"
        lines.append(f"| {finding.probe_id} | {finding.check} | {verdict} | {finding.observed} |")
    passed = sum(1 for finding in findings if finding.ok)
    lines.append(f"\n**{passed}/{len(findings)} checks passed.**")
    return "\n".join(lines) + "\n"


def _write_suite(directory: Path, report: str, evidence: dict[str, object]) -> None:
    """Write a suite's report and its raw evidence beside the transcripts it came from."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.md").write_text(report, encoding="utf-8")
    (directory / "evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _m12_probes(probe_dir: str | None, suite: str) -> list[Probe]:
    """The probes behind one M12 suite, read from that suite's own file.

    One file per suite rather than `load_probes` over the whole directory: three suites share
    `live_m12_probe_dir` and each must ask only its own questions, so a directory-wide read would
    put the routing corpus through the plan-gate protocol. The directory *is* still gated as a
    whole — `tests/test_m12_probes.py` runs `load_probes` across it, so a duplicate id between two
    suites is exactly as fatal here as it is in the corpus, and the schema is the same `ProbeSet`
    either way.

    Raises:
        FileNotFoundError: The suite's file is absent. Named rather than silently empty, because a
            suite that runs zero probes and reports zero failures is the coverage lie this entry
            point's exit code exists to prevent.
    """
    directory = Path(probe_dir if probe_dir is not None else settings.live_m12_probe_dir)
    path = directory / _M12_SUITES[suite]
    if not path.is_file():
        raise FileNotFoundError(f"suite {suite!r} needs {path}, which does not exist")
    return ProbeSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).probes


async def _run_plan_gate(args: argparse.Namespace) -> int:
    """Suite A — plan → approve → execute → re-gate, live. Exits non-zero on any failed check."""
    # Imported here rather than at module load: resolving the gated surface builds the connector
    # registry, and a `--suite corpus` run has no use for it.
    from chemclaw.agent.authz import side_effecting_tools

    gated = frozenset(side_effecting_tools())
    probes = _m12_probes(args.probe_dir, "plan-gate")
    directory = _suite_dir(args.transcript_dir, "plan-gate")
    runs: list[PlanGateRun] = []
    async with _client(args.base_url) as client:
        for probe in probes:
            logger.info("plan-gate probe %s: %d turn(s)", probe.id, len(probe.follow_ups) + 1)
            runs.append(await run_plan_gate_probe(client, probe, gated_tools=gated))

    findings = [finding for run in runs for finding in run.findings]
    report = _findings_report(
        "M12 · plan → approve → execute, live",
        "The GxP gate as a *conversation*: a write refused before approval, the same write running "
        "after it, and the plan changing out from under the decision (DARK-1).",
        findings,
        [
            f"front door `{args.base_url or settings.live_probe_base_url}`",
            f"{len(gated)} state-changing tool(s) the gate governs",
            f"transcripts in `{directory}`",
        ],
    )
    print(report)
    _write_suite(directory, report, {"runs": [run.model_dump() for run in runs]})
    return 0 if findings and all(finding.ok for finding in findings) else 1


async def _run_degradation(args: argparse.Namespace) -> int:
    """Suite B — `capability_degraded` must precede the first token, not merely exist."""
    probes = _m12_probes(args.probe_dir, "degradation")
    directory = _suite_dir(args.transcript_dir, "degradation")
    outcomes: list[ProbeOutcome] = []
    async with _client(args.base_url) as client:
        for probe in probes:
            outcomes.append(await run_turn(client, probe, message=probe.question))

    findings = [
        finding
        for probe, outcome in zip(probes, outcomes, strict=True)
        for finding in degradation_findings(probe, outcome)
    ]
    report = _findings_report(
        "M12 · durable-launcher ordering",
        "REV-6's claim, checked as an *ordering* rather than as a count: the outage has to be "
        "announced before the first token, or the model plans against a surface it will not get. "
        "Run this with the durable broker deliberately stopped.",
        findings,
        [
            f"front door `{args.base_url or settings.live_probe_base_url}`",
            f"transcripts in `{directory}`",
        ],
    )
    print(report)
    _write_suite(
        directory,
        report,
        {"outcomes": [outcome.model_dump() for outcome in outcomes]},
    )
    return 0 if findings and all(finding.ok for finding in findings) else 1


def _routing_report(scores: dict[str, RoutingScore]) -> str:
    """Both arms side by side, or one arm plus a note that the comparison is not yet possible.

    A single arm is deliberately *not* reported as an answer. The question M9 deferred its default
    to is comparative, and a team's accuracy with nothing to compare its cost against would be the
    same category of claim as "17/17 checks passed" over a matrix two families short.
    """
    lines = ["# M12 · team routing accuracy and per-specialist token cost\n"]
    lines.append(
        "Routing accuracy is scored over the turns that were *delegated*, and token cost over the "
        "turns the ledger could be asked about. Both denominators are printed, because the two "
        "failures they separate — never delegating, and delegating wrongly — have different fixes."
    )
    lines.append("")
    lines.append("| arm | probes | delegated | correct | accuracy | tokens | unmeasured turns |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for arm in sorted(scores):
        score = scores[arm]
        lines.append(
            f"| {arm} | {score.probes} | {score.routed} | {score.correct} | "
            f"{score.accuracy:.0%} | {score.total_tokens} | {score.unmeasured_turns} |"
        )

    for arm in sorted(scores):
        score = scores[arm]
        if not score.turns_by_specialist:
            continue
        lines.append(f"\n## {arm} · per specialist\n")
        lines.append("| specialist | turns | tokens | tokens/turn |")
        lines.append("| --- | ---: | ---: | ---: |")
        for name in sorted(score.turns_by_specialist):
            turns = score.turns_by_specialist[name]
            tokens = score.tokens_by_specialist.get(name, 0)
            lines.append(f"| {name} | {turns} | {tokens} | {tokens // max(turns, 1)} |")

    misroutes = {arm: score.misroutes for arm, score in scores.items() if score.misroutes}
    if misroutes:
        lines.append("\n## Mis-routes\n")
        for arm, rows in sorted(misroutes.items()):
            for probe_id, movement in sorted(rows.items()):
                lines.append(f"- **{arm}** {probe_id}: expected {movement}")

    if len(scores) < 2:
        lines.append(
            "\n**Only one arm has run.** Re-run with `--arm "
            f"{'single' if 'team' in scores else 'team'}` against a front door configured the "
            "other way (`CHEMCLAW_AGENT_TEAMS_ENABLED`) — a routing number with nothing to compare "
            "it against does not answer the question M9 deferred."
        )
    return "\n".join(lines) + "\n"


async def _run_routing(args: argparse.Namespace) -> int:
    """Suite C — one arm of the routing measurement, compared against the other when it exists."""
    probes = _m12_probes(args.probe_dir, "routing")
    directory = _suite_dir(args.transcript_dir, "routing")
    directory.mkdir(parents=True, exist_ok=True)

    outcomes: list[ProbeOutcome] = []
    async with _client(args.base_url) as client:
        for probe in probes:
            outcome = await run_turn(client, probe, message=probe.question)
            # Attached here rather than inside `run_turn`: the ledger read waits for a row that has
            # not landed yet, and paying that on all 190 corpus probes to serve one suite would be
            # the harness taxing the measurement it is not making.
            if outcome.session_id:
                outcome.tokens = await session_tokens(outcome.session_id)
            logger.info(
                "routing probe %s → %s",
                probe.id,
                outcome.specialists[0] if outcome.specialists else "(main agent)",
            )
            outcomes.append(outcome)

    score = score_routing(probes, outcomes, arm=args.arm)
    (directory / f"arm-{args.arm}.json").write_text(
        json.dumps(
            {
                "score": score.model_dump(),
                "outcomes": [outcome.model_dump() for outcome in outcomes],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    scores = {args.arm: score}
    for other in ("team", "single"):
        path = directory / f"arm-{other}.json"
        if other != args.arm and path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            scores[other] = RoutingScore.model_validate(stored["score"])

    report = _routing_report(scores)
    print(report)
    (directory / "summary.md").write_text(report, encoding="utf-8")

    if args.arm == "single":
        # The control arm has no routing to be right about — it exists to price the same questions
        # without a supervisor. Its gate is that it ran and was measurable, nothing more.
        return 0 if outcomes and score.unmeasured_turns < len(outcomes) else 1
    if not score.routed:
        logger.error(
            "the team arm delegated nothing: %d probe(s) were answered by the main agent, so "
            "there is no routing to score. Is CHEMCLAW_AGENT_TEAMS_ENABLED set on the front door?",
            len(outcomes),
        )
        return 1
    return 0 if score.accuracy >= settings.live_routing_accuracy_min else 1


async def _main(args: argparse.Namespace) -> int:
    if args.suite in _M12_SUITES:
        runner = {
            "plan-gate": _run_plan_gate,
            "degradation": _run_degradation,
            "routing": _run_routing,
        }[args.suite]
        return await runner(args)

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
    """Parse arguments and run the selected suite."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Run the live probe set against a running front door."
    )
    parser.add_argument(
        "--suite",
        default="corpus",
        choices=["corpus", *sorted(_M12_SUITES)],
        help="corpus (the 190-question run, default) or one M12 re-validation suite",
    )
    parser.add_argument(
        "--arm",
        default="team",
        choices=["team", "single"],
        help="routing suite only: which arm this front door is configured as",
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
