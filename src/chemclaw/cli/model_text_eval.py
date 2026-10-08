"""`python -m chemclaw.cli.model_text_eval` — does a batch of model-text edits ship?

The evaluation protocol of `D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`, run
in order: the offline gate (`make eval-strict`, `make eval-baseline-check`, the prose contract),
then the live A/B of the shipped text (control) against the candidate over the same probes, the
same gateway and at least `MINIMUM_RUNS` runs per arm, then `evals.model_text.ship_decision`.

The candidate arm is a second front door started with `CHEMCLAW_MODEL_TEXT_OVERLAY_DIR`
(`agent/text_overlay.py`), so a batch is measured before it is committed; a batch the overlay cannot
express (schema field text) runs from a checkout and brings its own `--candidate-inventory`.

Exit codes: 0 ship; 1 no ship, including a failed offline gate; 2 undecided (too few runs, an
ungradeable run, a misuse, or any `--dry-run`); 3 unreached (no gateway credential, no front door).
`--dry-run` drives the whole pipeline with deterministic fake answers, grades and spend; everything
it prints and writes says it is not evidence.
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
from chemclaw.cli.model_text_inventory import INVENTORY_PATH
from chemclaw.core.config import Settings, settings
from chemclaw.core.logging import configure_logging
from chemclaw.evals.live import ProbeOutcome, load_probes, run_probes
from chemclaw.evals.live_judge import Judgement, Verdict, judge_model
from chemclaw.evals.model_text import (
    METRIC_NAMES,
    MINIMUM_RUNS,
    GradedProbe,
    ShipDecision,
    TooFewRuns,
    render_table,
    run_metrics,
    ship_decision,
)
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)

EXIT_SHIP, EXIT_NO_SHIP, EXIT_UNDECIDED, EXIT_UNREACHED = 0, 1, 2, 3

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


def inventory_under_overlay(overlay: Path) -> dict[str, Any]:
    """The inventory a process running under `overlay` would write, produced by running one.

    A subprocess, because the overlay is read from `Settings` when the module is imported.
    """
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "inventory.json"
        done = subprocess.run(
            [sys.executable, "-m", "chemclaw.cli.model_text_inventory", "--output", str(target)],
            cwd=_ROOT,
            env={**os.environ, "CHEMCLAW_MODEL_TEXT_OVERLAY_DIR": str(overlay.resolve())},
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise Undecidable(
                f"could not measure the candidate prefix under {overlay}:\n{done.stderr[-800:]}"
            )
        return load_inventory(target)


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

    def __init__(self, candidate_shift: float = 0.0) -> None:
        """`candidate_shift` moves the candidate arm's odds; zero leaves the arms alike."""
        self._shift = candidate_shift

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
            if probe.bucket == "C":
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
) -> dict[str, list[dict[str, float | None]]]:
    """`runs` runs of each arm, alternating control then candidate so gateway drift reaches both.

    Raises:
        Undecidable: A run graded nothing, which means the judge failed and not the system.
    """
    metrics: dict[str, list[dict[str, float | None]]] = {CONTROL: [], CANDIDATE: []}
    for run in range(1, runs + 1):
        for arm in (CONTROL, CANDIDATE):
            graded = await driver.run_once(arm, run, probes)
            if not any(g.judgement and g.judgement.verdict != "ungraded" for g in graded):
                raise Undecidable(
                    f"{arm} run {run}: the judge graded none of {len(graded)} answers"
                )
            metrics[arm].append(run_metrics(graded))
            logger.info("%s run %d/%d: %s", arm, run, runs, metrics[arm][-1])
    return metrics


def decide(
    metrics: dict[str, list[dict[str, float | None]]], control_prefix: int, candidate_prefix: int
) -> ShipDecision:
    """`ship_decision` over the collected runs."""
    return ship_decision(
        {name: [run[name] for run in metrics[CONTROL]] for name in METRIC_NAMES},
        {name: [run[name] for run in metrics[CANDIDATE]] for name in METRIC_NAMES},
        control_prefix_tokens=control_prefix,
        candidate_prefix_tokens=candidate_prefix,
    )


def evidence_line(*, dry_run: bool, runs: int, probes: int) -> str:
    """What the table was measured on, printed above it; a dry run says it is not evidence."""
    if dry_run:
        return DRY_RUN_LABEL
    return (
        f"Live evaluation: gateway `{settings.llm_base_url}`, model `{settings.llm_model}`, judge "
        f"`{judge_model()}`; {runs} runs per arm over {probes} probes, control and candidate "
        "alternating."
    )


def gate_report(results: Sequence[GateResult]) -> str:
    """The offline gate as a list a reviewer reads before the live table."""
    lines = ["Offline gate:"]
    lines += [f"- {'pass' if r.ok else '**FAIL**'} `{r.name}`" for r in results]
    return "\n".join(lines)


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


def _candidate_inventory(args: argparse.Namespace, control: dict[str, Any]) -> dict[str, Any]:
    """The candidate's inventory: a file, or measured under its overlay.

    A dry run with neither borrows the control's prefix less a synthetic saving, so the pipeline
    still has a prefix to compare; the label already says the whole run is not evidence.

    Raises:
        Undecidable: There is nothing to measure the candidate's prefix from.
    """
    if args.candidate_inventory:
        return load_inventory(Path(args.candidate_inventory))
    if args.candidate_overlay:
        return inventory_under_overlay(Path(args.candidate_overlay))
    if args.dry_run:
        return {"prefix": {"total": prefix_tokens(control) - 100}}
    raise Undecidable("--candidate-overlay or --candidate-inventory is needed for the prefix")


async def evaluate(args: argparse.Namespace) -> int:
    """The protocol, start to finish. Returns the exit code."""
    dry_run: bool = args.dry_run
    directory = (
        Path(args.transcript_dir) if args.transcript_dir else run_output_dir("model-text-eval")
    )

    if not dry_run and not args.offline_only:
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

    gate: list[GateResult] = []
    if not args.skip_offline:
        gate = run_offline_gate()
        if any(not result.ok for result in gate):
            report = (
                gate_report(gate)
                + "\n\n**NO SHIP** — the offline gate failed; no live run was spent.\n"
            )
            print(report)
            write_results(directory, report=report, decision=None, metrics=None, provenance={})
            return EXIT_NO_SHIP
    if args.offline_only:
        print(gate_report(gate))
        return EXIT_SHIP

    probes = [p for p in load_probes(args.probe_dir) if p.bucket in set(args.buckets.split(","))]
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted or str(p.section) in wanted]
    if args.sample:
        probes = _systematic_sample(probes, args.sample)
    if not probes:
        logger.error("--buckets/--only/--sample selected no probes")
        return EXIT_UNDECIDED

    try:
        control_inventory = load_inventory(Path(args.control_inventory))
        candidate_inventory = _candidate_inventory(args, control_inventory)
    except Undecidable as exc:
        logger.error("undecided: %s", exc)
        return EXIT_UNDECIDED

    driver: ArmDriver
    if dry_run:
        driver = DryRunDriver(args.dry_run_shift)
    else:
        driver = LiveDriver({CONTROL: args.control_url, CANDIDATE: args.candidate_url}, directory)
    try:
        if not dry_run:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.live_probe_timeout_seconds), trust_env=False
            ) as client:
                overlay = Path(args.candidate_overlay) if args.candidate_overlay else None
                await check_arms(client, args.control_url, args.candidate_url, overlay)
        metrics = await collect(driver, probes, args.runs)
        decision = decide(
            metrics, prefix_tokens(control_inventory), prefix_tokens(candidate_inventory)
        )
    except Unreached as exc:
        logger.error("%s — this run measured nothing", exc)
        return EXIT_UNREACHED
    except (Undecidable, TooFewRuns, ValueError) as exc:
        logger.error("undecided: %s", exc)
        return EXIT_UNDECIDED

    evidence = evidence_line(dry_run=dry_run, runs=args.runs, probes=len(probes))
    report = "\n\n".join(
        part
        for part in (render_table(decision, evidence=evidence), gate_report(gate) if gate else "")
        if part
    )
    print(report)
    write_results(
        directory,
        report=report,
        decision=decision,
        metrics=metrics,
        provenance={
            "evidence": not dry_run,
            "probes": len(probes),
            "runs": args.runs,
            "control_url": None if dry_run else args.control_url,
            "candidate_url": None if dry_run else args.candidate_url,
            "candidate_overlay": args.candidate_overlay,
        },
    )
    logger.info("results written to %s", directory)
    if dry_run:
        logger.error(
            "a dry run is not evidence, so it exits %d whatever it decided", EXIT_UNDECIDED
        )
        return EXIT_UNDECIDED
    return EXIT_SHIP if decision.ship else EXIT_NO_SHIP


def _runs(value: str) -> int:
    """A run count that is at least `MINIMUM_RUNS`: the floor is not a flag's to lower."""
    parsed = int(value)
    if parsed < MINIMUM_RUNS:
        raise argparse.ArgumentTypeError(f"the spread needs at least {MINIMUM_RUNS} runs per arm")
    return parsed


def _positive(value: str) -> int:
    """A probe count that is at least 1."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(
        description="Decide whether a batch of model-text edits ships."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="fake answers, grades and spend: not evidence"
    )
    parser.add_argument(
        "--dry-run-shift",
        type=float,
        default=0.0,
        help="--dry-run only: move the candidate's odds (negative worse, positive better)",
    )
    parser.add_argument("--offline-only", action="store_true", help="run the offline gate and stop")
    parser.add_argument("--skip-offline", action="store_true", help="skip the offline gate")
    parser.add_argument(
        "--runs", type=_runs, default=MINIMUM_RUNS, help="runs per arm (at least 3)"
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
        help="an inventory written by `make model-text` in the candidate's checkout",
    )
    parser.add_argument(
        "--control-inventory", default=str(INVENTORY_PATH), help="the shipped inventory"
    )
    parser.add_argument("--probe-dir", default=None, help="override the configured probe directory")
    parser.add_argument(
        "--buckets", default="A,B,C", help="probe buckets to ask (C feeds refusals)"
    )
    parser.add_argument("--only", default=None, help="comma-separated probe ids or section numbers")
    parser.add_argument(
        "--sample", type=_positive, default=0, help="ask a systematic sample of N probes"
    )
    parser.add_argument("--transcript-dir", default=None, help="where transcripts and results land")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.offline_only and args.candidate_url is None:
        parser.error("--candidate-url is required (the candidate arm is a second front door)")
    if not args.dry_run and args.candidate_url == args.control_url:
        parser.error("--candidate-url equals --control-url: both arms would be the same front door")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the protocol and return its exit code."""
    configure_logging()
    return asyncio.run(evaluate(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
