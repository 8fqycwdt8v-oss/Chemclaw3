"""`python -m chemclaw.cli.live_probes` — run the live probe set against a running front door.

Everything printed is derived from transcripts already on disk. Coverage is reported apart from
quality: a run that answers every probe without calling a tool is not a good run, so tool reach is
printed beside the verdict counts, never folded into them.

`--suite` selects what is asked; the default is the corpus. The M12 suites (`plan-gate`,
`degradation`) each run their own probe file from `settings.live_m12_probe_dir` and are scored
mechanically. They share this entry point's client and transcript discipline so harnesses cannot
disagree about what a turn did. Every suite exits non-zero on a failed check or one it could not
take: a measurement that did not happen is not one that passed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import httpx
import yaml

from chemclaw.connectors.registry import job_names
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging
from chemclaw.core.markdown import render_table
from chemclaw.evals import delegation_run
from chemclaw.evals.ab import ABSummary, TaskScores
from chemclaw.evals.delegation import (
    BASELINE_ARM,
    MINIMUM_REPEATS,
    ArmRun,
    DelegationReport,
)
from chemclaw.evals.live import (
    Finding,
    PlanGateRun,
    ProbeOutcome,
    degradation_findings,
    load_probes,
    open_session,
    run_plan_gate_probe,
    run_probes,
    run_turn,
)
from chemclaw.evals.live_judge import (
    Judgement,
    judge_model,
    judge_outcome,
    judgement_from_transcript,
)
from chemclaw.evals.probe import Probe, ProbeSet
from chemclaw.evals.tool_utility import by_bucket, paired_tasks

logger = logging.getLogger(__name__)

# `_T` is the item type of the index-only `_systematic_sample`. `_AB_BASELINE_PROFILE` is the A/B
# control arm's shipped profile (`data/evals/profiles/no-tools.yaml`); a constant so "baseline"
# means one thing. It replaces the default instructions as well as the tools, so it is a prompt
# contrast, not a tools contrast (`tools-removed` varies only tools). `_M12_SUITES` maps each M12
# suite to its probe file, so a missing file fails with a searchable name.
_T = TypeVar("_T")

_AB_BASELINE_PROFILE = "no-tools"

_M12_SUITES: dict[str, str] = {
    "plan-gate": "plan_gate.yaml",
    "degradation": "degradation.yaml",
}


def _summary(
    probes: list[Probe],
    outcomes: list[ProbeOutcome],
    grades: list[Judgement],
    provenance: str,
) -> str:
    """The run in one table per axis: verdicts, tool reach, failure visibility, per section.

    `provenance` names what produced the numbers — the gateway a fresh run asked, or the transcript
    directory a re-grade read. A parameter, not read from `settings`, because a re-grade's verdicts
    belong to whatever gateway wrote the transcripts.
    """
    by_id = {p.id: p for p in probes}
    verdicts = Counter(g.verdict for g in grades)
    lines: list[str] = []

    lines.append(f"# Live probe run — {len(outcomes)} probes\n")
    lines.append(f"{provenance}\n")
    lines.append("## Verdicts\n")
    verdict_rows = []
    for verdict in ("served", "partial", "unserved", "fabricated", "ungraded"):
        count = verdicts.get(verdict, 0)
        verdict_rows.append([verdict, str(count), f"{count / max(len(grades), 1):.0%}"])
    lines.append(render_table(["verdict", "count", "share"], verdict_rows, align="lrr"))

    answered = sum(1 for o in outcomes if o.answered)
    zero_tool = [o for o in outcomes if not o.tools_called]
    zero_tool_covered = [o for o in zero_tool if by_id[o.probe_id].bucket == "A"]
    expected = [o for o in outcomes if o.expected_tools_met is not None]
    reached = [o for o in expected if o.expected_tools_met]
    silent = [o for o in outcomes if not o.answered and not o.failed_loudly]
    uncited = [o for o in outcomes if o.uncited_note_ids]

    lines.append("\n## Coverage and honesty\n")
    # Rows rather than text, so a signal nothing measured can be an absent row without dropping the
    # table.
    signals: list[list[str]] = [
        ["answered at all", f"{answered} / {len(outcomes)}"],
        ["expected tool reached", f"{len(reached)} / {len(expected)}"],
    ]
    # Gold-set mean recall over probes declaring `expects_notes`, kept apart from "expected tool
    # reached": a turn can reach `gather_evidence` and receive none of the relevant notes.
    graded_notes = [o for o in outcomes if o.expected_notes_recall is not None]
    if graded_notes:
        recalls = [o.expected_notes_recall or 0.0 for o in graded_notes]
        incomplete = sum(1 for o in graded_notes if o.expected_notes_missing)
        signals.append(
            [
                f"expected notes retrieved (mean recall over {len(graded_notes)} probes)",
                f"{sum(recalls) / len(recalls):.2f}",
            ]
        )
        signals.append(["…probes missing at least one expected note", str(incomplete)])
    signals += [
        ["answers using no tool at all", f"{len(zero_tool)} / {len(outcomes)}"],
        ["…of those, on questions the surface covers (bucket A)", str(len(zero_tool_covered))],
        ["**failed silently** (no answer, no error)", f"**{len(silent)}**"],
        ["**answers citing a note no tool returned**", f"**{len(uncited)}**"],
        [
            "clarified via ask_clarifying_question",
            str(sum(1 for o in outcomes if o.asked_clarifying)),
        ],
        [
            "…and clarified in prose instead (the tool existed)",
            str(sum(1 for o in outcomes if o.asked_clarifying_in_prose)),
        ],
        [
            "**answers opening on a critique the chemist never made**",
            f"**{sum(1 for o in outcomes if o.acknowledged_critique)}**",
        ],
        ["turns that surfaced a failure", str(sum(1 for o in outcomes if o.failed_loudly))],
        ["durable jobs started", str(sum(len(o.jobs_started) for o in outcomes))],
    ]

    # What the broker says became of declared jobs, beside the launch count: "started" and "ran" are
    # different facts. `RUNNING` is not a failure (a campaign outlives its turn), so states are
    # listed, not scored.
    job_states = Counter(state for outcome in outcomes for state in outcome.job_outcomes.values())
    if job_states:
        summary = " · ".join(f"{state} {count}" for state, count in sorted(job_states.items()))
        signals.append(["…and what Temporal says became of them", summary])

    # Whether an `expects_job` probe reached the durable path, asked of the tool calls rather than
    # of `job_started` events: a job that answers inside `inline_wait_seconds` is never announced,
    # so counting events would call a working durable path a miss.
    if any(p.expects_job for p in probes):
        jobs = set(job_names())
        ran_a_job = {o.probe_id for o in outcomes if o.job_outcomes or (jobs & set(o.tools_called))}
        inline = {
            o.probe_id for o in outcomes if not o.job_outcomes and (jobs & set(o.tools_called))
        }
        missed = sorted({p.id for p in probes if p.expects_job} - ran_a_job)
        if inline:
            signals.append(
                ["…of which finished inside the turn (never announced)", str(len(inline))]
            )
        if missed:
            signals.append(
                [
                    "**probes needing a durable job that ran none**",
                    f"**{', '.join(missed)}**",
                ]
            )

    latencies = sorted(o.latency_seconds for o in outcomes)
    if latencies:
        signals.append(["median turn", f"{latencies[len(latencies) // 2]:.1f} s"])
    lines.append(render_table(["signal", "value"], signals, align="lr"))

    lines.append("\n## By bucket\n")
    grade_by_id = {g.probe_id: g for g in grades}
    bucket_rows = []
    for bucket in ("A", "B", "C"):
        ids = [p.id for p in probes if p.bucket == bucket]
        counts = Counter(grade_by_id[i].verdict for i in ids if i in grade_by_id)
        bucket_rows.append(
            [bucket, str(len(ids))]
            + [
                str(counts.get(verdict, 0))
                for verdict in ("served", "partial", "unserved", "fabricated", "ungraded")
            ]
        )
    lines.append(
        render_table(
            ["bucket", "probes", "served", "partial", "unserved", "fabricated", "ungraded"],
            bucket_rows,
            align="lrrrrrr",
        )
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


def _gateway_line() -> str:
    """The model gateway this run resolved, named in the report and warned about when it is a mock.

    Asked of `Settings` and `cli.mock_llm` rather than compared against a string written here, so it
    follows the default if the default moves.
    """
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    line = f"**model gateway**: {settings.llm_base_url} · **model**: {settings.llm_model}"
    if _scripted_gateway():
        logger.warning(
            "this run is pointed at the scripted mock (%s). Its answers are a fixed script, so "
            "nothing here can be graded and the run will exit non-zero.",
            MOCK_BASE_URL,
        )
        return line + " — **the scripted mock**: its answers are not gradeable"
    return line


def _reachability_status(outcomes: list[ProbeOutcome]) -> int:
    """3 when the run reached the front door for no probe at all; 0 otherwise.

    `transport_error` is the signal because it means the request never arrived; a front door
    answering 500 is a result to report, not a refusal. Exit 3 matches
    `validate_template_args_live`'s "could not reach something"; 2 is `_grading_status`' "reached it
    and graded nothing". All, not any: a partial outage is reported per probe.
    """
    if outcomes and all(o.transport_error for o in outcomes):
        logger.error(
            "not one of the %d probes reached %s — this run measured nothing",
            len(outcomes),
            settings.live_probe_base_url,
        )
        return 3
    return 0


def _grading_status(
    grades: list[Judgement], outcomes: list[ProbeOutcome], *, scripted: bool
) -> int:
    """2 when no judge produced a verdict, or when the gateway was the scripted mock; 0 otherwise.

    A run where the judge graded nothing has measured nothing. The boundary is zero rather than a
    configured floor: a partial ungraded share is a real result, and no measurement supports any
    threshold between.

    An `unserved` on a turn with no answer is recorded without asking the judge, so it is not a
    verdict; a verdict counts only when the outcome carries an answer. A run against the scripted
    mock is always 2 (`scripted`): its answers and judge are the same fixed script, so its verdicts
    are evidence about the mock.
    """
    answered = {outcome.probe_id for outcome in outcomes if outcome.answered}
    judged = [g for g in grades if g.verdict != "ungraded" and g.probe_id in answered]
    if scripted:
        logger.error(
            "this run's gateway was the scripted mock, so its %d judgement(s) grade a script. "
            "Point CHEMCLAW_LLM_BASE_URL at a gateway to measure the system.",
            len(grades),
        )
        return 2
    if judged:
        return 0
    logger.error(
        "none of the %d judgements is a verdict a judge returned (ungraded, or unserved on a "
        "turn that never answered) — this run measured nothing. The usual cause is a gateway "
        "that cannot grade: `settings.llm_base_url` is %s.",
        len(grades),
        settings.llm_base_url,
    )
    return 2


def _scripted_gateway() -> bool:
    """Whether this process is pointed at `cli.mock_llm` — asked of it, never transcribed."""
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    return settings.llm_base_url == MOCK_BASE_URL


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

    So one run cannot overwrite another's results. A run that graded nothing writes no grades file,
    since an empty one is indistinguishable from every answer failing.
    """
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "summary.md").write_text(report, encoding="utf-8")
    if grades:
        (transcript_dir / "grades.json").write_text(
            json.dumps([g.model_dump() for g in grades], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _client(base_url: str | None) -> httpx.AsyncClient:
    """A front-door client on the configured timeout — one construction, so all suites agree.

    Sends `live_probe_token` as a bearer when configured and no Authorization header otherwise: a
    dev-posture front door never reads it, and an empty token means the lane is not enforcing
    identity.
    """
    # `.get_secret_value()`, because an f-string does not unwrap a `SecretStr`: formatting the
    # field itself compiles, runs, and sends `**********` to a front door that answers 401.
    headers = (
        {"Authorization": f"Bearer {settings.live_probe_token.get_secret_value()}"}
        if settings.live_probe_token.get_secret_value()
        else {}
    )
    return httpx.AsyncClient(
        base_url=base_url if base_url is not None else settings.live_probe_base_url,
        timeout=httpx.Timeout(settings.live_probe_timeout_seconds),
        headers=headers,
        # The client carries a bearer, so an ambient proxy must not receive it;
        # `tests/test_netguard.py` holds every client to `trust_env=False`.
        trust_env=False,
    )


#: One stamp per process, computed at import, so all of one run's transcripts and its summary land
#: in the same directory.
_RUN_STAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def run_output_dir(suite: str) -> Path:
    """`<live_probe_transcript_dir>/<suite>/<this run's UTC stamp>` — where a live run writes.

    The parent stays the committed transcripts directory (`.gitignore` exempts it so live results
    can be read back); a per-run subdirectory means no run overwrites the record or dirties a
    tracked file, and promoting a run is a deliberate copy. `live_jobs` and `live_data` write
    through this function too.
    """
    return Path(settings.live_probe_transcript_dir) / suite / _RUN_STAMP


def _suite_dir(transcript_dir: str | None, suite: str) -> Path:
    """Where one suite's transcripts and report land.

    A subdirectory per suite, then one per run (`run_output_dir`), so outputs sit with the
    transcripts that produced them and suites cannot overwrite each other.
    """
    if transcript_dir is not None:
        return Path(transcript_dir)
    return run_output_dir(suite)


def _findings_report(title: str, preamble: str, findings: list[Finding], notes: list[str]) -> str:
    """The shared shape of a suite report: what ran, what was observed, and what was not taken.

    A check that could not be taken is a row with `ok=False` and an `observed` saying why, never an
    absent row.
    """
    lines = [f"# {title}\n", preamble, ""]
    lines.extend(f"- {note}" for note in notes)
    lines.append("")
    lines.append(
        render_table(
            ["probe", "check", "result", "observed"],
            [
                [
                    finding.probe_id,
                    finding.check,
                    "PASS" if finding.ok else "**FAIL**",
                    finding.observed,
                ]
                for finding in findings
            ],
        )
    )
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

    The suites share `live_m12_probe_dir` and each must ask only its own questions;
    `tests/test_m12_probes.py` still validates the whole directory with `load_probes`.

    Raises:
        FileNotFoundError: The suite's file is absent, rather than a silent zero-probe run.
    """
    directory = Path(probe_dir if probe_dir is not None else settings.live_m12_probe_dir)
    path = directory / _M12_SUITES[suite]
    if not path.is_file():
        raise FileNotFoundError(f"suite {suite!r} needs {path}, which does not exist")
    return ProbeSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).probes


async def _run_plan_gate(args: argparse.Namespace) -> int:
    """Suite A — plan → approve → execute → re-gate, live. Exits non-zero on any failed check.

    Exits 3 against the scripted mock before a probe is asked: `cli.mock_llm` never writes a plan,
    so the scenario cannot be staged and any verdict would be about the mock.
    """
    if _scripted_gateway():
        logger.error(
            "the plan-gate suite cannot be staged against the scripted mock (%s): it never writes "
            "a plan, so there is nothing to approve and no check here can be reached. Point "
            "CHEMCLAW_LLM_BASE_URL at a gateway and start the lane with "
            "CHEMCLAW_HARNESS_AUTONOMY=plan_only.",
            settings.llm_base_url,
        )
        return 3
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
        "The approval gate as a *conversation*: a write refused before approval, the same write "
        "running after it, and the plan changing out from under the decision (DARK-1).",
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


def _ab_report(
    probes: list[Probe],
    summaries: dict[str, ABSummary],
    dropped: list[str],
    tasks: list[TaskScores],
) -> str:
    """The A/B's report: what was asked, what each bucket says, and every per-probe delta.

    The per-probe table says which questions the control arm paid on, the only actionable part. The
    heading names the variable rather than "with and without tools", since `_AB_BASELINE_PROFILE`
    swaps the system prompt as well as the tools.
    """
    lines = [
        "# Control-arm utility: the same questions, both arms",
        "",
        f"- probes asked in both arms: **{len(probes)}**",
        f"- pairs scored: **{len(tasks)}**"
        + (f" ({len(dropped)} dropped ungraded: {', '.join(dropped)})" if dropped else ""),
        f"- baseline arm: `{_AB_BASELINE_PROFILE}` — `tool_names: []` **and** its own"
        " `instructions:`, so this delta varies prompt and tools together",
        f"- judge: `{judge_model()}`",
        "",
        render_table(
            ["set", "n", "helped", "hurt", "no effect", "net delta"],
            [
                [
                    name,
                    str(len(summary.utilities)),
                    str(len(summary.helped)),
                    str(len(summary.hurt)),
                    str(len(summary.no_effect)),
                    f"{summary.net_delta:+.4g}",
                ]
                for name, summary in summaries.items()
            ],
        ),
    ]
    bucket_of = {probe.id: probe.bucket for probe in probes}
    lines += [
        "",
        "## Per probe",
        "",
        render_table(
            ["probe", "bucket", "baseline", "augmented", "delta"],
            [
                [
                    task.task_id,
                    bucket_of[task.task_id],
                    f"{task.baseline:+.1f}",
                    f"{task.augmented:+.1f}",
                    f"{task.augmented - task.baseline:+.1f}",
                ]
                for task in tasks
            ],
        ),
    ]
    return "\n".join(lines) + "\n"


async def _grade_all(probes: list[Probe], outcomes: list[ProbeOutcome]) -> dict[str, Judgement]:
    """Grade one arm's outcomes, keyed by probe id — the shape `paired_tasks` pairs on."""
    by_id = {probe.id: probe for probe in probes}
    semaphore = asyncio.Semaphore(settings.live_probe_concurrency)

    async def grade(outcome: ProbeOutcome) -> Judgement:
        async with semaphore:
            return await judge_outcome(by_id[outcome.probe_id], outcome)

    graded = await asyncio.gather(*(grade(outcome) for outcome in outcomes))
    return {judgement.probe_id: judgement for judgement in graded}


async def _assert_profiles(base_url: str | None, profiles: Sequence[str]) -> None:
    """Refuse to start unless the front door actually knows every arm profile this run will ask for.

    Checked before any model call: a front door missing `data/evals/profiles` on
    `CHEMCLAW_PROFILES_DIR` would reject baseline turns after the other arm was paid for, or run
    identical arms. `get_profile` raises on an unknown name, so one session open per profile
    suffices. The default agent is not checked.
    """
    async with _client(base_url) as client:
        for profile in dict.fromkeys(profiles):
            try:
                session_id = await open_session(client, profile=profile)
            except httpx.HTTPStatusError as exc:
                raise SystemExit(
                    f"the front door does not accept profile {profile!r} ({exc}). "
                    "Start it with CHEMCLAW_PROFILES_DIR=data/profiles:data/evals/profiles — "
                    "every arm here is a profile, and without it the arms would be one agent."
                ) from exc
            # Delete the probe session so it leaves no `session_owners` row; best-effort, since
            # failing to delete is no reason to refuse the measurement.
            with contextlib.suppress(httpx.HTTPError):
                (await client.delete(f"/sessions/{session_id}")).raise_for_status()


def _systematic_sample(probes: list[_T], count: int) -> list[_T]:
    """`count` items spread evenly across `probes`, in corpus order; everything if it is smaller.

    Generic over indices so `tests/test_tool_utility.py` can pin its ends and middle with integers.
    """
    if count >= len(probes):
        return probes
    return [probes[i * len(probes) // count] for i in range(count)]


async def _run_ab(args: argparse.Namespace) -> int:
    """Ask each selected probe twice — default agent, then toolless — and compare the verdicts.

    Each arm is an ordinary `run_probes` over its own transcript directory, inspectable with every
    transcript tool, plus one report pairing them.
    """
    probes = [p for p in load_probes(args.probe_dir) if p.bucket in set(args.buckets.split(","))]
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted or str(p.section) in wanted]
    if args.limit:
        probes = probes[: args.limit]
    if args.sample:
        # A systematic sample, not the first N: the corpus is in section order, so `[:N]` would
        # cover one or two sections. Evenly spaced indices are reproducible without a seed, so
        # reruns compare.
        probes = _systematic_sample(probes, args.sample)
    if not probes:
        logger.error("--buckets/--only/--limit/--sample selected no probes")
        return 2

    await _assert_profiles(args.base_url, [_AB_BASELINE_PROFILE])
    directory = _suite_dir(args.transcript_dir, "ab")
    logger.info("A/B over %d probes: augmented arm first", len(probes))
    augmented_outcomes = await run_probes(
        probes, base_url=args.base_url, transcript_dir=str(directory / "augmented")
    )
    logger.info("A/B: baseline arm (%s)", _AB_BASELINE_PROFILE)
    baseline_outcomes = await run_probes(
        probes,
        base_url=args.base_url,
        transcript_dir=str(directory / "baseline"),
        profile=_AB_BASELINE_PROFILE,
    )

    augmented = await _grade_all(probes, augmented_outcomes)
    baseline = await _grade_all(probes, baseline_outcomes)
    tasks, dropped = paired_tasks(probes, augmented, baseline)
    if not tasks:
        logger.error("every pair was ungraded — the judge failed, not the system under test")
        return 2
    summaries = by_bucket(probes, tasks)
    report = _ab_report(probes, summaries, dropped, tasks)
    print(report)
    _write_suite(
        directory,
        report,
        {
            "tasks": [task.model_dump() for task in tasks],
            "dropped_ungraded": dropped,
            "summaries": {name: s.model_dump() for name, s in summaries.items()},
            "augmented": [j.model_dump() for j in augmented.values()],
            "baseline": [j.model_dump() for j in baseline.values()],
        },
    )
    return 0


def _marked(probe: Probe, behaviour: str) -> Probe:
    """`probe` with the mock's behaviour selector on its question — the scripted double only.

    A copy, since the corpus object is shared across arms and repeats. The marker goes on `question`
    so it also reaches the judge (`evals/live_judge._prompt` quotes it), whose call goes to the same
    gateway. Against a real gateway nothing is marked.
    """
    return probe.model_copy(update={"question": f"[[{behaviour}]] {probe.question}"})


def _mock_behaviour_overrides(raw: Sequence[str], mock: bool) -> dict[str, str]:
    """`--mock-behaviour arm=behaviour`, refused unless the gateway *is* the scripted double.

    Drives the double through compliance states it cannot otherwise produce (`contaminated`,
    `undelegated`) so the comparator's buckets are exercised; against a real model it would script
    the result, so it is refused there.

    Raises:
        SystemExit: An override was given against a real gateway, or is not `arm=behaviour`.
    """
    overrides: dict[str, str] = {}
    for item in raw:
        arm, _, behaviour = item.partition("=")
        if not arm or not behaviour:
            raise SystemExit(f"--mock-behaviour wants arm=behaviour, got {item!r}")
        overrides[arm] = behaviour
    if overrides and not mock:
        raise SystemExit(
            f"--mock-behaviour only applies to the scripted mock; this run resolved "
            f"{settings.llm_base_url} as its gateway. Overriding a real model's behaviour is not "
            "something a flag can do, and pretending to would script the result rather than the "
            "double."
        )
    return overrides


async def _drive_delegation_arm(
    spec: delegation_run.ArmSpec,
    probes: list[Probe],
    args: argparse.Namespace,
    directory: Path,
    behaviour: str,
    mock: bool,
) -> list[delegation_run.ArmRepeat]:
    """Ask every probe `--repeats` times on one arm, grading as each pass lands.

    One ordinary `run_probes` per repeat, each into its own transcript directory.
    """
    records: list[delegation_run.ArmRepeat] = []
    asked = [_marked(probe, behaviour) if mock else probe for probe in probes]
    for repeat in range(1, args.repeats + 1):
        logger.info("delegation arm %s, repeat %d/%d", spec.arm, repeat, args.repeats)
        outcomes = await run_probes(
            asked,
            base_url=args.base_url,
            transcript_dir=str(directory / spec.arm / f"repeat-{repeat}"),
            profile=spec.profile,
        )
        graded = await _grade_all(asked, outcomes)
        records.extend(
            delegation_run.ArmRepeat(
                arm=spec.arm,
                repeat=repeat,
                outcome=outcome,
                judgement=graded[outcome.probe_id],
            )
            for outcome in outcomes
        )
    return records


def _delegation_provenance(
    specs: Sequence[delegation_run.ArmSpec], overrides: Mapping[str, str], mock: bool
) -> list[str]:
    """The lines a reader needs before any figure below them means anything.

    First, a run against the scripted double says so: its numbers are evidence about this runner,
    not about delegation.
    """
    lines = [_gateway_line(), f"judge: `{judge_model()}`"]
    if mock:
        lines.append(
            "**this run is against the scripted double, so every figure below is evidence about "
            "the runner and none of it is evidence about whether delegation pays**"
        )
    for spec in specs:
        behaviour = overrides.get(spec.arm, spec.mock_behaviour)
        lines.append(
            f"arm `{spec.arm}`: profile `{spec.profile}`, treatment `{spec.treatment}`, "
            f"front door needs {spec.posture}"
            + (f", scripted double behaviour `{behaviour}`" if mock else "")
        )
    return lines


def _write_delegation(
    directory: Path,
    runs: Sequence[ArmRun],
    run_set: delegation_run.ArmRunSet,
    reports: Mapping[str, DelegationReport],
    refused: Mapping[str, str],
    report: str,
) -> None:
    """Write the runs, the report and the raw evidence beside the transcripts that produced them.

    `runs.json` is written first and always: it is the expensive part (one real turn per task, arm
    and repeat) and what `--compare-runs` reads back, so a refused comparison does not lose it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "runs.json").write_text(
        json.dumps([run.model_dump() for run in runs], indent=2), encoding="utf-8"
    )
    _write_suite(
        directory,
        report,
        {
            "runs": [run.model_dump() for run in runs],
            "ungraded": run_set.ungraded,
            "unbilled": run_set.unbilled,
            "reports": {arm: report.model_dump() for arm, report in reports.items()},
            "not_reported": dict(refused),
        },
    )


async def _run_delegation(args: argparse.Namespace) -> int:
    """Suite D — the delegation experiment's run half: drive every arm, record what each turn did.

    Exits 3 when nothing reached the front door, 2 when no arm carried a comparison, 0 otherwise. No
    pass/fail: a negative result is as legitimate as a positive one.
    """
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    probes = delegation_run.load_delegation_probes(args.probe_dir)
    if args.only:
        wanted = set(args.only.split(","))
        probes = [probe for probe in probes if probe.id in wanted]
    if args.limit:
        probes = probes[: args.limit]
    if not probes:
        logger.error("--only/--limit selected no probes from the delegation corpus")
        return 2

    directory = _suite_dir(args.transcript_dir, "delegation")
    mock = settings.llm_base_url == MOCK_BASE_URL

    if args.compare_runs:
        # Aggregate recorded observations without asking anything — the only way to assemble a pair
        # whose repeats differ in whether they delegated.
        runs = delegation_run.load_recorded_runs(
            [Path(item) for item in args.compare_runs.split(",")]
        )
        run_set = delegation_run.ArmRunSet(runs=list(runs))
        arms = sorted({run.arm for run in runs})
        provenance = [
            f"**aggregated** from {len(runs)} recorded run(s) in `{args.compare_runs}` — "
            "nothing was asked of any gateway by this invocation",
            f"arms present: {', '.join(arms)}",
        ]
    else:
        try:
            specs = [delegation_run.arm_by_name(name) for name in args.arms.split(",")]
        except KeyError as exc:
            # Reported rather than raised: a typo in `--arms` is a misinvocation, and a traceback
            # for one reads like a defect in the runner.
            logger.error("%s", exc)
            return 2
        if not any(spec.arm == BASELINE_ARM for spec in specs):
            logger.error(
                "--arms must include the baseline %r; every report is against it", BASELINE_ARM
            )
            return 2
        overrides = _mock_behaviour_overrides(args.mock_behaviour, mock)
        await _assert_profiles(args.base_url, [spec.profile for spec in specs])
        records: list[delegation_run.ArmRepeat] = []
        for spec in specs:
            records.extend(
                await _drive_delegation_arm(
                    spec,
                    probes,
                    args,
                    directory,
                    overrides.get(spec.arm, spec.mock_behaviour),
                    mock,
                )
            )
        if all(record.outcome.transport_error for record in records):
            logger.error(
                "not one of the %d turn(s) reached %s — this run measured nothing",
                len(records),
                args.base_url or settings.live_probe_base_url,
            )
            return 3
        sessions = [record.outcome.session_id for record in records if record.outcome.session_id]
        ran = await delegation_run.tools_that_ran(sessions)
        billed = await delegation_run.billed_by_session_when_booked(sessions)
        run_set = delegation_run.assemble_runs(
            records, ran, billed, {spec.arm: spec.treatment for spec in specs}
        )
        runs = run_set.runs
        arms = [spec.arm for spec in specs]
        provenance = _delegation_provenance(specs, overrides, mock)

    # `MINIMUM_REPEATS`, not a flag: the comparator's floor for a meaningful median must not be
    # lowerable from the command line.
    reports, refused = delegation_run.compare_every_arm(runs, arms, MINIMUM_REPEATS)
    report = delegation_run.render_report(reports, runs, run_set, provenance)
    for arm, why in refused.items():
        report += f"\n**no report for `{arm}`**: {why}\n"
    print(report)
    _write_delegation(directory, runs, run_set, reports, refused, report)
    logger.info("%d run(s), %d report(s) written to %s", len(runs), len(reports), directory)
    if not reports:
        logger.error(
            "no arm carried a comparison — %d run(s) recorded. A report over an empty set would "
            "read as 'no effect anywhere'.",
            len(runs),
        )
        return 2
    if mock and not args.compare_runs:
        # A run against the scripted double exits non-zero even when every arm reported: the double
        # decides to delegate, so the run proves only that the runner observes delegation. Outputs
        # are written first.
        logger.error(
            "this run's gateway was the scripted mock, so its %d report(s) are evidence about this "
            "runner and not about delegation. Point CHEMCLAW_LLM_BASE_URL at a gateway to measure "
            "the question.",
            len(reports),
        )
        return 2
    return 0


async def _main(args: argparse.Namespace) -> int:
    if args.suite == "ab":
        return await _run_ab(args)
    if args.suite == "delegation":
        return await _run_delegation(args)
    if args.suite in _M12_SUITES:
        runner = {
            "plan-gate": _run_plan_gate,
            "degradation": _run_degradation,
        }[args.suite]
        return await runner(args)

    if args.regrade:
        # Re-grade stored transcripts without re-asking, so a grader fix does not change the
        # subject.
        directory = Path(args.transcript_dir or settings.live_probe_transcript_dir)
        probes, outcomes = _load_transcripts(directory)
        if not outcomes:
            # A re-grade over no transcripts measured nothing; it must not write an empty summary
            # over the committed record and exit 0.
            logger.error("no transcripts to re-grade in %s — nothing was measured", directory)
            return 2
        logger.info("re-grading %d stored transcripts from %s", len(outcomes), directory)
        by_id = {p.id: p for p in probes}
        semaphore = asyncio.Semaphore(settings.live_probe_concurrency)

        async def regrade(outcome: ProbeOutcome) -> Judgement:
            async with semaphore:
                return await judge_outcome(by_id[outcome.probe_id], outcome)

        regraded: list[Judgement] = list(await asyncio.gather(*(regrade(o) for o in outcomes)))
        report = _summary(
            probes,
            outcomes,
            regraded,
            f"**re-graded** from {len(outcomes)} stored transcripts in `{directory}`, "
            f"by `{judge_model()}`. The answers themselves are whatever gateway wrote them.",
        )
        print(report)
        _write_outputs(directory, report, regraded)
        return _grading_status(regraded, outcomes, scripted=_scripted_gateway())

    probes = load_probes(args.probe_dir)
    loaded = len(probes)
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted or str(p.section) in wanted]
    if args.limit:
        probes = probes[: args.limit]
    if not probes:
        # Zero probes is a run that measured nothing, as the M12 suites treat it; a renamed probe id
        # must not turn `--only` into a permanent green line.
        logger.error(
            "--only/--limit selected no probes out of %d loaded from %s",
            loaded,
            args.probe_dir or settings.live_probe_dir,
        )
        return 2
    logger.info(
        "running %d probes against %s", len(probes), args.base_url or settings.live_probe_base_url
    )

    # Resolved once and passed to both writers, so transcripts and summary land in the same
    # directory.
    directory = _suite_dir(args.transcript_dir, "corpus")
    outcomes = await run_probes(probes, base_url=args.base_url, transcript_dir=str(directory))

    grades: list[Judgement] = []
    if not args.no_judge:
        by_id = {p.id: p for p in probes}
        semaphore = asyncio.Semaphore(settings.live_probe_concurrency)

        async def grade(outcome: ProbeOutcome) -> Judgement:
            async with semaphore:
                return await judge_outcome(by_id[outcome.probe_id], outcome)

        grades = list(await asyncio.gather(*(grade(o) for o in outcomes)))

    report = _summary(probes, outcomes, grades, _gateway_line())
    print(report)

    _write_outputs(directory, report, grades)
    logger.info("transcripts and summary written to %s", directory)

    # Reachability first, and it binds every run: a pass that reached nothing measured nothing
    # whether or not it asked for verdicts.
    unreachable = _reachability_status(outcomes)
    if unreachable:
        return unreachable
    # `--no-judge` exits 0: it asked for no verdicts and still measured coverage, tool reach, silent
    # failures and job launches. The empty-selection case above still applies.
    return 0 if args.no_judge else _grading_status(grades, outcomes, scripted=_scripted_gateway())


def _positive(value: str) -> int:
    """A probe count argparse will not accept as zero or negative.

    Feeds `probes[: args.limit]`, where 0 selects nothing and -1 silently drops the last probe. Kept
    local rather than shared with `leak_probe._positive`: the shared part is smaller than an import.
    """
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The probe lane's command line, split out of `main` so it can be tested without a live run."""
    # `configure_logging()`, not `basicConfig`: it installs the `SecretRedactingFilter` that scrubs
    # the `live_probe_token` bearer from logged errors and tracebacks (`tests/test_logging.py`).
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Run the live probe set against a running front door."
    )
    parser.add_argument(
        "--suite",
        default="corpus",
        choices=["corpus", "ab", "delegation", *sorted(_M12_SUITES)],
        help=(
            "corpus (the default single-arm run), ab (the same probes in both arms, the baseline "
            "one being a profile that swaps the prompt as well as the tools), delegation (the "
            "delegation experiment's arms over the delegation corpus), or one M12 re-validation "
            "suite"
        ),
    )
    parser.add_argument(
        "--arms",
        default=",".join(spec.arm for spec in delegation_run.ARMS),
        help=(
            "--suite delegation only: which arms to drive, comma-separated. Must include "
            f"{BASELINE_ARM!r}, because every report is against it."
        ),
    )
    parser.add_argument(
        "--repeats",
        type=_positive,
        default=MINIMUM_REPEATS,
        help=(
            "--suite delegation only: repeats per (task, arm). Defaults to "
            "`evals.delegation.MINIMUM_REPEATS`, which is where the floor is argued; the "
            "comparator's own floor is not lowered by this flag."
        ),
    )
    parser.add_argument(
        "--mock-behaviour",
        action="append",
        default=[],
        metavar="ARM=BEHAVIOUR",
        help=(
            "--suite delegation only, and only against `cli.mock_llm`: drive one arm through a "
            "different scripted behaviour, so a compliance state a deterministic double cannot "
            "otherwise reach (a baseline that delegates, a treatment arm that does not) can be "
            "driven. Refused against a real gateway."
        ),
    )
    parser.add_argument(
        "--compare-runs",
        default=None,
        metavar="PATH[,PATH]",
        help=(
            "--suite delegation only: aggregate recorded `runs.json` files and report, asking "
            "nothing of any gateway"
        ),
    )
    parser.add_argument(
        "--sample",
        type=_positive,
        default=0,
        help=(
            "--suite ab only: ask a systematic sample of N probes spread across the selected "
            "buckets, rather than every one of them"
        ),
    )
    parser.add_argument(
        "--buckets",
        default="A,C",
        help=(
            "--suite ab only: which buckets to compare. The default is the pair the comparison "
            "is about — A is where tools should win, C is where they are an opportunity to "
            "fabricate a capability that does not exist."
        ),
    )
    parser.add_argument("--probe-dir", default=None, help="override the configured probe directory")
    parser.add_argument("--base-url", default=None, help="front door base URL")
    parser.add_argument("--transcript-dir", default=None, help="where transcripts are written")
    parser.add_argument("--only", default=None, help="comma-separated probe ids or section numbers")
    parser.add_argument(
        "--limit", type=_positive, default=0, help="run at most N probes (at least 1)"
    )
    parser.add_argument(
        "--no-judge", action="store_true", help="skip grading (mechanical signals only)"
    )
    parser.add_argument(
        "--regrade",
        action="store_true",
        help="re-grade stored transcripts without re-running any probe",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected suite and return its exit code."""
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
