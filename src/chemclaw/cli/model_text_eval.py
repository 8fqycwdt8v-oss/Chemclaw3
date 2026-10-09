"""`python -m chemclaw.cli.model_text_eval` — does a batch of model-text edits ship?

The evaluation protocol of `D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`, as
refined by `D-2026-10-08-the-text-evaluation-states-its-power-and-holds-the-prefix-to-its-claim`,
run in order: the offline gate (`make eval-strict`, `make eval-baseline-check`, the prose
contract), then the live A/B of the shipped text (control) against the candidate over the same
probes and gateway, at least `--min-runs` runs per arm, then `evals.model_text.ship_decision`.

The candidate arm is a second front door started with `CHEMCLAW_MODEL_TEXT_OVERLAY_DIR`
(`agent/text_overlay.py`), so a batch is measured before it is committed; a batch the overlay cannot
express (schema field text) runs from a checkout and brings its own `--candidate-inventory`, which
must have been measured in this environment. A live run states its spend and starts only when the
operator types it back (`--confirm-turns`).

Exit codes: 10 ship, which only a live run whose offline gate passed returns; 0 the offline gate
passed and nothing was shipped (`--offline-only`); 1 no ship; 2 undecided (too few runs, an
ungradeable or lopsided run, a refusal to spend, a misuse, or any `--dry-run`); 3 unreached (no
gateway credential, no front door); 4 a live table that would ship with the offline gate skipped.
`--dry-run` drives the pipeline with deterministic fake answers, grades and spend; everything it
prints and writes says it is not evidence.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

from chemclaw.agent.text_overlay import DIGEST_CHARS, load_overlay
from chemclaw.cli.live_probes import _grade_all, _systematic_sample, run_output_dir
from chemclaw.cli.mock_llm import MOCK_BASE_URL
from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import configure_logging
from chemclaw.evals.live import ProbeOutcome, load_probes, run_probes
from chemclaw.evals.live_judge import Judgement, Verdict, judge_model
from chemclaw.evals.model_text import (
    DEFAULT_MAX_DROP_SHARE,
    DEFAULT_PREFIX_TOLERANCE_TOKENS,
    METRIC_NAMES,
    SHIP_MINIMUM_RUNS,
    GradedProbe,
    ShipDecision,
    TooFewRuns,
    render_table,
    restrict_to_common,
    run_metrics,
    ship_decision,
)
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)

EXIT_OFFLINE_PASSED, EXIT_NO_SHIP, EXIT_UNDECIDED, EXIT_UNREACHED, EXIT_UNGATED = 0, 1, 2, 3, 4
#: The one code that means "ship", distinct from 0 so that no run that merely finished returns it.
EXIT_SHIP = 10

#: A live run asks this many probes unless told otherwise, and refuses to plan more turns than
#: `DEFAULT_MAX_TURNS`: fifty probes, five runs, two arms. Both are flags a person raises on
#: purpose.
DEFAULT_SAMPLE = 50
DEFAULT_MAX_TURNS = 500

_ROOT = Path(__file__).resolve().parents[3]
CONTROL, CANDIDATE = "control", "candidate"

#: Printed first on, and written into, every dry-run output.
DRY_RUN_LABEL: Final = (
    "**NOT EVIDENCE — dry run.** Answers, grades and spend below are deterministic fakes that "
    "exercise the pipeline's plumbing. They say nothing about any text and must not be pasted "
    "into a pull request as an evaluation."
)

#: The three settings a live arm needs, by the environment variable that sets each.
CREDENTIAL_SETTINGS: Final = ("CHEMCLAW_LLM_BASE_URL", "CHEMCLAW_LLM_MODEL", "CHEMCLAW_LLM_API_KEY")

#: The offline gate, cheapest first. Each runs from the repository root.
OFFLINE_GATE: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("eval-strict", ("make", "eval-strict")),
    ("eval-baseline-check", ("make", "eval-baseline-check")),
    (
        "prose-contract",
        (
            "uv",
            "run",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            "0",
            "tests/test_prose_contract.py",
        ),
    ),
)


class Unreached(RuntimeError):
    """No probe of a run reached its front door, so the run measured nothing."""


class Undecidable(RuntimeError):
    """The runs cannot support a decision (for instance the judge graded nothing)."""


# ----------------------------------------------------------------------------- preconditions


def credential_gap() -> list[str]:
    """The environment variables of `CREDENTIAL_SETTINGS` this process has not set to a gateway.

    Asked of `Settings`, so a `.env` counts and the defaults (the loopback mock, the model `mock`,
    no key) are exactly what is missing. The judge runs in this process, so it needs them as much as
    the front doors do.
    """
    defaults = Settings.model_fields
    missing = []
    if settings.llm_base_url in {MOCK_BASE_URL, defaults["llm_base_url"].default}:
        missing.append("CHEMCLAW_LLM_BASE_URL")
    if settings.llm_model == defaults["llm_model"].default:
        missing.append("CHEMCLAW_LLM_MODEL")
    if not settings.llm_api_key.get_secret_value():
        missing.append("CHEMCLAW_LLM_API_KEY")
    return missing


@dataclass(frozen=True)
class GateResult:
    """One offline check: its name, whether it passed, and the tail of its output."""

    name: str
    ok: bool
    tail: str


def run_offline_gate(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[GateResult]:
    """Run `OFFLINE_GATE`, every check even after one fails, so the report shows all of them.

    `run` is `subprocess.run`; injected so a test can drive the pipeline without paying for the
    gate.
    """
    results = []
    for name, command in OFFLINE_GATE:
        done = run(command, cwd=_ROOT, capture_output=True, text=True, check=False)
        tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-6:])
        results.append(GateResult(name, done.returncode == 0, tail))
    return results


def load_inventory(path: Path) -> dict[str, Any]:
    """An inventory file, which must carry its per-request prefix.

    Raises:
        Undecidable: The file is missing or is not an inventory.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        total = int(loaded["prefix"]["total"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Undecidable(f"{path} is not a readable model-text inventory ({exc})") from exc
    if total <= 0:
        raise Undecidable(f"{path} reports a per-request prefix of {total} tokens")
    return dict(loaded)


def measure_inventory(overlay: Path | None) -> dict[str, Any]:
    """The inventory a process under `overlay` (or none) would write, produced by running one.

    A subprocess, because the overlay is read from `Settings` when the module is imported, and the
    same one for both arms: same interpreter, same working directory, same environment and `.env`,
    differing only in the overlay variable (set for the candidate, emptied for the control). Two
    prefixes are comparable only if they were measured this way.
    """
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "inventory.json"
        done = subprocess.run(
            [sys.executable, "-m", "chemclaw.cli.model_text_inventory", "--output", str(target)],
            cwd=_ROOT,
            env={
                **os.environ,
                "CHEMCLAW_MODEL_TEXT_OVERLAY_DIR": ""
                if overlay is None
                else str(overlay.resolve()),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise Undecidable(
                f"could not measure the prefix ({'shipped text' if overlay is None else overlay}):"
                f"\n{done.stderr[-800:]}"
            )
        return load_inventory(target)


def check_inventories_agree(
    control: dict[str, Any], candidate: dict[str, Any], overlay: Path | None
) -> None:
    """Refuse two prefixes that were not measured in the same environment, or of the wrong text.

    Each inventory records the settings that decide the surface (`environment`) and the overlay it
    was built under. They must agree on the first, the control must have no overlay, and a
    candidate said to be an overlay must carry that overlay's digest.

    Raises:
        Undecidable: The inventories disagree, or predate the fields.
    """
    for name, inventory in (("control", control), ("candidate", candidate)):
        if "environment" not in inventory or "overlay" not in inventory:
            raise Undecidable(f"the {name} inventory predates `environment`; regenerate it")
    differing = sorted(
        key
        for key in {*control["environment"], *candidate["environment"]}
        if control["environment"].get(key) != candidate["environment"].get(key)
    )
    if control.get("python") != candidate.get("python"):
        differing.append("python")
    if differing:
        raise Undecidable(
            "the two prefixes were measured in different environments, so a difference between "
            f"them may be the environment's: {differing}. Measure both here, with "
            "no --control-inventory or --candidate-inventory."
        )
    if control["overlay"] is not None:
        raise Undecidable(f"the control inventory was built under overlay {control['overlay']}")
    expected = (
        None if overlay is None else load_overlay(str(overlay.resolve())).digest[:DIGEST_CHARS]
    )
    if overlay is not None and candidate["overlay"] != expected:
        raise Undecidable(
            f"the candidate inventory was built under overlay {candidate['overlay']}, not the "
            f"{expected} of {overlay}"
        )


def prefix_tokens(inventory: dict[str, Any]) -> int:
    """`prefix.total` of a loaded inventory."""
    return int(inventory["prefix"]["total"])


# --------------------------------------------------------------------------------- the drivers


class ArmDriver(Protocol):
    """Asks every probe once of one arm and returns each answer graded and costed."""

    async def run_once(self, arm: str, run: int, probes: list[Probe]) -> list[GradedProbe]:
        """One run of one arm."""
        ...


class LiveDriver:
    """The real thing: two front doors, the judge, and the booked ledger.

    Records come from what `evals.live` already writes per probe plus the ledger rows
    (`turn_costs`) of the sessions the run opened, so tokens and cost are what was billed.
    """

    def __init__(self, urls: dict[str, str], directory: Path) -> None:
        """`urls` maps each arm to its front door; transcripts go under `directory`."""
        self._urls = urls
        self._directory = directory

    async def run_once(self, arm: str, run: int, probes: list[Probe]) -> list[GradedProbe]:
        """Ask `probes` of the arm's front door, grade them, and join the ledger."""
        from chemclaw.evals.delegation_run import spend_by_session_when_booked

        outcomes = await run_probes(
            probes,
            base_url=self._urls[arm],
            transcript_dir=str(self._directory / arm / f"run-{run}"),
        )
        if all(outcome.transport_error for outcome in outcomes):
            raise Unreached(f"no probe reached {self._urls[arm]} ({arm} run {run})")
        grades = await _grade_all(probes, outcomes)
        spend = await spend_by_session_when_booked([o.session_id for o in outcomes if o.session_id])
        by_id = {probe.id: probe for probe in probes}
        graded = []
        for outcome in outcomes:
            booked = spend.get(outcome.session_id)
            graded.append(
                GradedProbe(
                    by_id[outcome.probe_id],
                    outcome,
                    grades.get(outcome.probe_id),
                    None if booked is None else booked.tokens,
                    None if booked is None else booked.billed,
                )
            )
        return graded


class DryRunDriver:
    """Deterministic fake answers, grades and spend: the plumbing's test double, never evidence.

    Every draw is a hash of (arm, run, probe, purpose), so a run is reproducible and the arms differ
    by sampling noise alone, unless `candidate_shift` moves the candidate's odds to exercise a
    verdict: negative makes it worse, positive better.
    """

    def __init__(self, candidate_shift: float = 0.0, candidate_drop: float = 0.0) -> None:
        """`candidate_shift` moves the candidate arm's odds; `candidate_drop` fails its grading."""
        self._shift = candidate_shift
        self._drop = candidate_drop

    @staticmethod
    def _draw(*parts: object) -> float:
        digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    async def run_once(self, arm: str, run: int, probes: list[Probe]) -> list[GradedProbe]:
        """Fake one run: the same questions, answered by arithmetic."""
        shift = self._shift if arm == CANDIDATE else 0.0

        def draw(probe: Probe, purpose: str) -> float:
            return self._draw(arm, run, probe.id, purpose)

        graded = []
        for probe in probes:
            met = draw(probe, "tool") < 0.80 + shift if probe.expects_tools else None
            first_calls = 1 + int(draw(probe, "calls") * 3)
            bad = draw(probe, "args") > 0.90 + shift
            roll = draw(probe, "verdict")
            verdict: Verdict
            if arm == CANDIDATE and draw(probe, "drop") < self._drop:
                verdict = "ungraded"
            elif probe.bucket == "C":
                verdict = "served" if roll < 0.70 + shift else "fabricated"
            else:
                verdict = (
                    "served" if roll < 0.55 + shift else "partial" if roll < 0.80 else "unserved"
                )
            tokens = int(8_000 * (1 - shift) + draw(probe, "tokens") * 4_000)
            outcome = ProbeOutcome(
                probe_id=probe.id,
                section=probe.section,
                persona=probe.persona,
                bucket=probe.bucket,
                question=probe.question,
                answered=True,
                expected_tools_met=met,
                first_calls=first_calls,
                first_call_argument_errors=["fake_tool"] if bad else [],
            )
            graded.append(
                GradedProbe(
                    probe,
                    outcome,
                    Judgement(probe_id=probe.id, verdict=verdict),
                    tokens,
                    round(tokens * 1.1),
                )
            )
        return graded


# ------------------------------------------------------------------------------ the evaluation


async def _overlay_digest(client: httpx.AsyncClient, url: str) -> str | None:
    """The overlay digest a front door reports on `/readyz`, or `None` when it runs shipped text.

    Raises:
        Unreached: The door did not answer, or did not answer in JSON.
    """
    try:
        body = (await client.get(f"{url.rstrip('/')}/readyz")).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise Unreached(f"{url} did not answer /readyz ({type(exc).__name__})") from exc
    digest = body.get("model_text_overlay") if isinstance(body, dict) else None
    return str(digest) if digest else None


async def check_arms(
    client: httpx.AsyncClient, control_url: str, candidate_url: str, overlay: Path | None
) -> None:
    """Confirm each front door runs the text its arm names, before a probe is spent.

    An instrument that cannot tell its arms apart reads "no effect" whatever the text: a candidate
    door started without the overlay is a second control. So the control must report no overlay,
    and the candidate the digest of the overlay this command was given (or none, when the candidate
    is a checkout, whose identity this cannot read).

    Raises:
        Unreached: A door does not answer `/readyz`.
        Undecidable: The arms do not run what they are labelled with.
    """
    control = await _overlay_digest(client, control_url)
    candidate = await _overlay_digest(client, candidate_url)
    if control is not None:
        raise Undecidable(f"the control front door {control_url} runs overlay {control}")
    expected = None
    if overlay is not None:
        expected = load_overlay(str(overlay.resolve())).digest[:DIGEST_CHARS]
    if candidate != expected:
        raise Undecidable(
            f"the candidate front door {candidate_url} reports overlay {candidate}, and this "
            f"evaluation was given {expected or 'no overlay'}: the arms would not differ as "
            "labelled"
        )


async def collect(
    driver: ArmDriver, probes: list[Probe], runs: int
) -> dict[str, list[list[GradedProbe]]]:
    """`runs` runs of each arm, alternating control then candidate so gateway drift reaches both.

    Returns every probe's record, completed or not: which probes the comparison stands on is
    `restrict_to_common`'s decision, made once over all the runs.

    Raises:
        Undecidable: A run graded nothing, which means the judge failed and not the system.
    """
    raw: dict[str, list[list[GradedProbe]]] = {CONTROL: [], CANDIDATE: []}
    for run in range(1, runs + 1):
        for arm in (CONTROL, CANDIDATE):
            graded = await driver.run_once(arm, run, probes)
            if not any(g.judgement and g.judgement.verdict != "ungraded" for g in graded):
                raise Undecidable(
                    f"{arm} run {run}: the judge graded none of {len(graded)} answers"
                )
            raw[arm].append(graded)
            logger.info("%s run %d/%d: %d probes", arm, run, runs, len(graded))
    return raw


def decide(
    raw: dict[str, list[list[GradedProbe]]],
    control_prefix: int,
    candidate_prefix: int,
    *,
    claims_token_saving: bool,
    prefix_tolerance: int,
    minimum_runs: int,
    max_drop_share: float,
) -> tuple[ShipDecision, dict[str, list[dict[str, float | None]]]]:
    """Put both arms on the probes both completed, measure each run, and apply the rule.

    Returns the decision and the per-run metrics it was made from.
    """
    kept, coverage = restrict_to_common(raw)
    metrics = {arm: [run_metrics(one) for one in runs] for arm, runs in kept.items()}
    decision = ship_decision(
        {name: [run[name] for run in metrics[CONTROL]] for name in METRIC_NAMES},
        {name: [run[name] for run in metrics[CANDIDATE]] for name in METRIC_NAMES},
        control_prefix_tokens=control_prefix,
        candidate_prefix_tokens=candidate_prefix,
        claims_token_saving=claims_token_saving,
        prefix_tolerance_tokens=prefix_tolerance,
        minimum_runs=minimum_runs,
        coverage=coverage,
        max_drop_share=max_drop_share,
    )
    return decision, metrics


def evidence_line(*, dry_run: bool, runs: int, probes: int) -> str:
    """What the table was measured on, printed above it; a dry run says it is not evidence."""
    if dry_run:
        return DRY_RUN_LABEL
    return (
        f"Live evaluation: gateway `{settings.llm_base_url}`, model `{settings.llm_model}`, judge "
        f"`{judge_model()}`; {runs} runs per arm over {probes} probes, control and candidate "
        "alternating."
    )


def gate_report(results: Sequence[GateResult], *, skipped: bool = False) -> str:
    """The offline gate as a list a reviewer reads before the live table."""
    if skipped:
        return (
            "Offline gate: **SKIPPED** (`--skip-offline`). A run without its gate cannot be a "
            "ship, whatever the table says."
        )
    lines = ["Offline gate:"]
    lines += [f"- {'pass' if r.ok else '**FAIL**'} `{r.name}`" for r in results]
    return "\n".join(lines)


@dataclass(frozen=True)
class Plan:
    """What a live run will spend, stated before it spends it."""

    probes: int
    runs: int

    @property
    def turns(self) -> int:
        """Agent turns: every probe, every run, both arms. The judge is asked about each answer."""
        return self.probes * self.runs * 2

    def sentence(self) -> str:
        """The plan as the line printed before any spend."""
        return (
            f"Planned spend: {self.probes} probes x {self.runs} runs x 2 arms = {self.turns} agent "
            f"turns and up to {self.turns} judge calls."
        )


def check_plan(plan: Plan, *, max_turns: int, confirmed: int, charged: bool) -> str | None:
    """A refusal to start, or `None` when the plan is within its ceiling and confirmed.

    A dry run spends nothing and needs neither. Otherwise the plan must fit under `max_turns`
    (raising it is the explicit act) and the operator must have typed the number it comes to.
    """
    if not charged:
        return None
    if plan.turns > max_turns:
        return (
            f"{plan.sentence()} That is over --max-turns {max_turns}. Narrow it with --sample or "
            "--runs, or raise --max-turns to say you mean it."
        )
    if confirmed != plan.turns:
        return f"{plan.sentence()} Re-run with --confirm-turns {plan.turns} to spend it."
    return None


def exit_code(decision: ShipDecision, *, dry_run: bool, gate_passed: bool) -> int:
    """The exit code for a finished comparison.

    Only a live run whose offline gate passed can return `EXIT_SHIP`. A would-be ship without the
    gate is `EXIT_UNGATED`; a dry run is always `EXIT_UNDECIDED`; a verdict that is only
    underpowered is undecided, not a no.
    """
    if dry_run:
        return EXIT_UNDECIDED
    if decision.ship:
        return EXIT_SHIP if gate_passed else EXIT_UNGATED
    if decision.underpowered and decision.reason.startswith("underpowered"):
        return EXIT_UNDECIDED
    return EXIT_NO_SHIP


def write_results(
    directory: Path,
    *,
    report: str,
    decision: ShipDecision | None,
    metrics: dict[str, list[dict[str, float | None]]] | None,
    provenance: dict[str, object],
) -> None:
    """Write `summary.md` and `results.json` beside the transcripts of this run."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.md").write_text(report, encoding="utf-8")
    payload = {
        "provenance": provenance,
        "runs": metrics,
        "decision": None if decision is None else decision.model_dump(),
    }
    (directory / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _inventories(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both arms' inventories, measured the same way unless the operator brings files.

    A dry run with no candidate overlay or file borrows the control's with a synthetic saving, so
    the pipeline still has a prefix to compare; the label says the whole run is not evidence.

    Raises:
        Undecidable: A prefix cannot be measured, or the two were not measured alike.
    """
    overlay = Path(args.candidate_overlay) if args.candidate_overlay else None
    control = (
        load_inventory(Path(args.control_inventory))
        if args.control_inventory
        else measure_inventory(None)
    )
    if args.candidate_inventory:
        candidate = load_inventory(Path(args.candidate_inventory))
    elif overlay is not None:
        candidate = measure_inventory(overlay)
    elif args.dry_run:
        candidate = {**control, "prefix": {"total": prefix_tokens(control) - 100}}
    else:
        raise Undecidable("--candidate-overlay or --candidate-inventory is needed for the prefix")
    check_inventories_agree(control, candidate, overlay)
    return control, candidate


def _select_probes(args: argparse.Namespace) -> list[Probe]:
    """The probes to ask: the chosen buckets, narrowed by `--only`, then sampled evenly."""
    probes = [p for p in load_probes(args.probe_dir) if p.bucket in set(args.buckets.split(","))]
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted or str(p.section) in wanted]
    return _systematic_sample(probes, args.sample) if args.sample else probes


async def evaluate(args: argparse.Namespace) -> int:
    """The protocol, start to finish. Returns the exit code."""
    dry_run: bool = args.dry_run
    live = not dry_run and not args.offline_only
    directory = (
        Path(args.transcript_dir) if args.transcript_dir else run_output_dir("model-text-eval")
    )

    if live:
        gap = credential_gap()
        if gap:
            logger.error(
                "the live arm needs a gateway this process can reach, and %s %s not set. The "
                "judge runs here and the front doors need the same three: CHEMCLAW_LLM_BASE_URL, "
                "CHEMCLAW_LLM_MODEL and CHEMCLAW_LLM_API_KEY. Nothing was measured, and a batch "
                "does not ship without it.",
                " and ".join(gap),
                "is" if len(gap) == 1 else "are",
            )
            return EXIT_UNREACHED
        if args.candidate_url is None:
            logger.error("--candidate-url is required: the candidate arm is a second front door")
            return EXIT_UNDECIDED
        if args.candidate_url == args.control_url:
            logger.error("--candidate-url equals --control-url: both arms would be one front door")
            return EXIT_UNDECIDED

    probes: list[Probe] = []
    if not args.offline_only:
        probes = _select_probes(args)
        if not probes:
            logger.error("--buckets/--only/--sample selected no probes")
            return EXIT_UNDECIDED
        plan = Plan(len(probes), args.runs)
        print(plan.sentence())
        refusal = check_plan(
            plan, max_turns=args.max_turns, confirmed=args.confirm_turns, charged=live
        )
        if refusal:
            logger.error("%s", refusal)
            return EXIT_UNDECIDED

    gate: list[GateResult] = []
    if not args.skip_offline:
        gate = run_offline_gate()
        if any(not result.ok for result in gate):
            report = (
                gate_report(gate)
                + "\n\n**NO SHIP** — the offline gate failed; no live run was spent.\n"
            )
            print(report)
            write_results(
                directory,
                report=report,
                decision=None,
                metrics=None,
                provenance={"offline_gate": "failed", "ship_exit_code": EXIT_SHIP},
            )
            return EXIT_NO_SHIP
    if args.offline_only:
        print(
            gate_report(gate)
            + "\n\n**OFFLINE GATE PASSED. This is not a ship verdict**: no live run was made, so "
            f"nothing here can ship (a ship exits {EXIT_SHIP}; this exits {EXIT_OFFLINE_PASSED}).\n"
        )
        return EXIT_OFFLINE_PASSED

    try:
        control_inventory, candidate_inventory = _inventories(args)
        driver: ArmDriver
        if dry_run:
            driver = DryRunDriver(args.dry_run_shift, args.dry_run_drop)
        else:
            driver = LiveDriver(
                {CONTROL: args.control_url, CANDIDATE: args.candidate_url}, directory
            )
            overlay = Path(args.candidate_overlay) if args.candidate_overlay else None
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.live_probe_timeout_seconds), trust_env=False
            ) as client:
                await check_arms(client, args.control_url, args.candidate_url, overlay)
        raw = await collect(driver, probes, args.runs)
        decision, metrics = decide(
            raw,
            prefix_tokens(control_inventory),
            prefix_tokens(candidate_inventory),
            claims_token_saving=args.claims_token_saving,
            prefix_tolerance=args.prefix_tolerance,
            minimum_runs=args.min_runs,
            max_drop_share=args.max_drop_share,
        )
    except Unreached as exc:
        logger.error("%s — this run measured nothing", exc)
        return EXIT_UNREACHED
    except (Undecidable, TooFewRuns, ValueError) as exc:
        logger.error("undecided: %s", exc)
        return EXIT_UNDECIDED

    gate_passed = not args.skip_offline
    evidence = evidence_line(dry_run=dry_run, runs=args.runs, probes=len(probes))
    report = "\n\n".join(
        [render_table(decision, evidence=evidence), gate_report(gate, skipped=args.skip_offline)]
    )
    code = exit_code(decision, dry_run=dry_run, gate_passed=gate_passed)
    print(report)
    write_results(
        directory,
        report=report,
        decision=decision,
        metrics=metrics,
        provenance={
            "evidence": not dry_run,
            "offline_gate": "passed" if gate_passed else "skipped",
            "exit_code": code,
            "ship_exit_code": EXIT_SHIP,
            "probes": len(probes),
            "runs": args.runs,
            "planned_turns": Plan(len(probes), args.runs).turns,
            "claims_token_saving": args.claims_token_saving,
            "prefix_tolerance_tokens": args.prefix_tolerance,
            "control_url": None if dry_run else args.control_url,
            "candidate_url": None if dry_run else args.candidate_url,
            "candidate_overlay": args.candidate_overlay,
            "candidate_overlay_digest": candidate_inventory.get("overlay"),
            "environment": control_inventory.get("environment"),
        },
    )
    logger.info("results written to %s", directory)
    if dry_run:
        logger.error("a dry run is not evidence, so it exits %d whatever it decided", code)
    elif decision.ship and not gate_passed:
        logger.error(
            "the table would ship, but the offline gate was skipped: exit %d, not %d",
            code,
            EXIT_SHIP,
        )
    return code


def _at_least(floor: int, what: str) -> Callable[[str], int]:
    """An argparse type for a count that may be raised above `floor` and never lowered."""

    def parse(value: str) -> int:
        parsed = int(value)
        if parsed < floor:
            raise argparse.ArgumentTypeError(f"{what} must be at least {floor}")
        return parsed

    return parse


def _positive(value: str) -> int:
    """A count that is at least 1."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _share(value: str) -> float:
    """A share between 0 and 1."""
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(
        description="Decide whether a batch of model-text edits ships.",
        epilog=(
            f"Exit codes: {EXIT_SHIP} ship (live, offline gate passed); {EXIT_OFFLINE_PASSED} "
            f"offline gate passed, no live run; {EXIT_NO_SHIP} no ship; {EXIT_UNDECIDED} undecided "
            f"or a dry run; {EXIT_UNREACHED} unreached; {EXIT_UNGATED} a live table that would "
            "ship, with the offline gate skipped."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="fake answers, grades and spend: not evidence"
    )
    parser.add_argument(
        "--dry-run-shift", type=float, default=0.0, help="--dry-run: candidate odds shift"
    )
    parser.add_argument(
        "--dry-run-drop",
        type=_share,
        default=0.0,
        help="--dry-run: share of candidate probes the fake judge fails to grade",
    )
    parser.add_argument("--offline-only", action="store_true", help="run the offline gate and stop")
    parser.add_argument("--skip-offline", action="store_true", help="skip the offline gate")
    parser.add_argument(
        "--min-runs",
        type=_at_least(SHIP_MINIMUM_RUNS, "--min-runs"),
        default=SHIP_MINIMUM_RUNS,
        help="runs per arm below which no ship can be returned (can be raised, not lowered)",
    )
    parser.add_argument(
        "--runs", type=_positive, default=None, help="runs per arm (default --min-runs)"
    )
    parser.add_argument(
        "--claims-token-saving",
        action="store_true",
        help="the batch says it saves tokens, so the prefix must shrink; otherwise it may not grow "
        "past --prefix-tolerance",
    )
    parser.add_argument(
        "--prefix-tolerance",
        type=_at_least(0, "--prefix-tolerance"),
        default=DEFAULT_PREFIX_TOLERANCE_TOKENS,
        help="tokens the per-request prefix may grow when no saving is claimed",
    )
    parser.add_argument(
        "--max-drop-share",
        type=_share,
        default=DEFAULT_MAX_DROP_SHARE,
        help="the most probes an arm may fail to complete before the comparison is refused",
    )
    parser.add_argument(
        "--max-turns",
        type=_positive,
        default=DEFAULT_MAX_TURNS,
        help="refuse to start a live run planned over this many agent turns; raise it to mean it",
    )
    parser.add_argument(
        "--confirm-turns",
        type=int,
        default=0,
        help="the planned turn count, typed back: a live run does not start without it",
    )
    parser.add_argument(
        "--control-url", default=settings.live_probe_base_url, help="shipped-text front door"
    )
    parser.add_argument("--candidate-url", default=None, help="candidate-text front door")
    parser.add_argument(
        "--candidate-overlay",
        default=None,
        help="the overlay directory the candidate front door runs under; measures its prefix",
    )
    parser.add_argument(
        "--candidate-inventory",
        default=None,
        help="an inventory written by `make model-text` in the candidate's checkout, here",
    )
    parser.add_argument(
        "--control-inventory",
        default=None,
        help="a pre-measured shipped inventory; by default it is measured here, like the candidate",
    )
    parser.add_argument("--probe-dir", default=None, help="override the configured probe directory")
    parser.add_argument(
        "--buckets", default="A,B,C", help="probe buckets to ask (C feeds refusals)"
    )
    parser.add_argument("--only", default=None, help="comma-separated probe ids or section numbers")
    parser.add_argument(
        "--sample",
        type=int,
        default=DEFAULT_SAMPLE,
        help=f"ask a systematic sample of N probes (default {DEFAULT_SAMPLE}; 0 asks all of them)",
    )
    parser.add_argument("--transcript-dir", default=None, help="where transcripts and results land")
    args = parser.parse_args(argv)
    if args.runs is None:
        args.runs = args.min_runs
    if args.runs < args.min_runs:
        parser.error(f"--runs {args.runs} is below --min-runs {args.min_runs}: it could never ship")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the protocol and return its exit code."""
    configure_logging()
    return asyncio.run(evaluate(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
