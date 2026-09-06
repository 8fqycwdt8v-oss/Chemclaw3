"""Entry points that mutate something must answer their command line before they do it.

`tests/test_validator_entrypoints.py` holds this rule for the read-only gates. This file holds it
for the two commands in `chemclaw.cli` that reach a broker or a database, plus the reporting
command an operator runs beside them.

The defect these were written against is one shape seen three times. `chemclaw.cli.schedules` had
no `argparse` at all — `asyncio.run(main())` ran under `if __name__ == "__main__"` and every
argument was discarded, so `python -m chemclaw.cli.schedules --help` *applied the Temporal
Schedules* and exited 0: asking a broker-mutating command what it does performed it.
`chemclaw.cli.explain` read `sys.argv[1]` raw, so `--help` and `--not-a-flag` were both looked up
as session ids under a heading naming the flag, and `""` matched the rows written before the
correlation id existed — another actor's audit rows and durable jobs, printed under a blank
heading. `validate_kg`'s own docstring records this exact fault as fixed one directory over.

Nothing here connects to Temporal: `_apply` is stubbed to fail if it is reached, which is what
makes "parsed the argument instead of applying" an assertion rather than a claim.
"""

from pathlib import Path
from typing import Any

import pytest


def _unreachable(*_args: Any, **_kwargs: Any) -> Any:
    """A stand-in for the broker: reaching it at all is the failure being tested for."""
    raise AssertionError("the schedules applier was reached; this command applied something")


def test_the_schedule_applier_answers_help_without_applying_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--help` must print help. It used to apply the Schedules and exit 0."""
    from chemclaw.cli import schedules

    monkeypatch.setattr(schedules, "_apply", _unreachable)
    with pytest.raises(SystemExit) as raised:
        schedules.main(["--help"])
    assert raised.value.code == 0


def test_the_schedule_applier_refuses_an_argument_it_does_not_understand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--delete-everything` applied the Schedules and exited 0. It is now a usage error."""
    from chemclaw.cli import schedules

    monkeypatch.setattr(schedules, "_apply", _unreachable)
    with pytest.raises(SystemExit) as raised:
        schedules.main(["--delete-everything"])
    assert raised.value.code == 2  # argparse's own "bad usage" status


def test_the_schedule_applier_can_show_its_plan_without_a_broker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` reports what an apply would do and reaches nothing.

    Derivable offline because `planned_schedules()` is pure by design and the prune set is
    `OWNED_SCHEDULE_IDS` minus the planned ids — the same arithmetic `_prune` uses.
    """
    from chemclaw.cli import schedules
    from chemclaw.durable.schedules import OWNED_SCHEDULE_IDS, planned_schedules

    monkeypatch.setattr(schedules, "_apply", _unreachable)
    assert schedules.main(["--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "dry run: nothing was applied" in printed
    for job in planned_schedules():
        assert job.schedule_id in printed
    for stale in OWNED_SCHEDULE_IDS - {job.schedule_id for job in planned_schedules()}:
        assert stale in printed


def test_applying_stays_the_default_because_a_container_invokes_it_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No arguments must still apply.

    Deliberately *unlike* its data-touching siblings, which preview by default. This one is a
    container command in `deploy/helm/chemclaw/templates/schedules-job.yaml`, invoked with no
    arguments — a preview-by-default would turn every deployment's Schedule Job into a no-op that
    exits 0, which is the same green-while-doing-nothing failure moved rather than fixed.
    """
    from chemclaw.cli import schedules

    applied: list[bool] = []

    async def _record() -> None:
        applied.append(True)

    monkeypatch.setattr(schedules, "_apply", _record)
    assert schedules.main([]) == 0
    assert applied == [True]


def test_the_audit_reconstruction_answers_help_rather_than_looking_it_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--help` was reported as a session with no messages, tool calls or jobs, at exit 0."""
    from chemclaw.cli import explain

    monkeypatch.setattr(explain, "explain", _unreachable)
    with pytest.raises(SystemExit) as raised:
        explain.main(["--help"])
    assert raised.value.code == 0


def test_the_audit_reconstruction_refuses_a_flag_shaped_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown flag is a usage error, not a session id looked up under its own name."""
    from chemclaw.cli import explain

    monkeypatch.setattr(explain, "explain", _unreachable)
    with pytest.raises(SystemExit) as raised:
        explain.main(["--not-a-flag"])
    assert raised.value.code == 2


def test_the_audit_reconstruction_refuses_a_blank_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blank id is the disclosure arm, not just a usability one.

    Rows written before the correlation id was recorded carry `session_id = ''`, so `explain ""` —
    which is what an unset shell variable expands to — printed another actor's audit rows and
    durable jobs under a blank heading. Refused for the reason `erase_actor` refuses a blank
    actor.
    """
    from chemclaw.cli import explain

    monkeypatch.setattr(explain, "explain", _unreachable)
    assert explain.main([""]) == 64


def test_an_unreachable_database_is_one_line_rather_than_a_stack_trace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`core.db` publishes `ConnectionError` for an unreachable database; this reports it.

    An operator reading a stack trace out of a read-only reporting command learns only that it
    crashed. The exit code was already 1 and stays 1 — what changes is what they can act on.
    """
    from chemclaw.cli import explain

    async def _unreachable_db(_session_id: str) -> list[str]:
        raise ConnectionError("Postgres unreachable at host=localhost port=5999")

    monkeypatch.setattr(explain, "explain", _unreachable_db)
    assert explain.main(["a-session"]) == 1
    assert "cannot read the audit trail" in capsys.readouterr().err


def test_a_damaged_soak_record_is_reported_rather_than_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated record is the expected damaged input, not an exotic one.

    `infra/live/soak.sh` appends a line per round while a long run is in flight, so an operator
    reporting on a run that was killed mid-write gets a half-written line. That surfaced as a
    `json.decoder.JSONDecodeError` traceback; the missing-file case one line above was already
    handled cleanly, which is the split this closes. A directory argument reached `read_text` the
    same way and raised `IsADirectoryError`.
    """
    from chemclaw.cli import soak_report

    damaged = tmp_path / "record.jsonl"
    damaged.write_text('{"round": 1, "api": {"rss_kb": 1}}\n{"round": 2, "ap\n', encoding="utf-8")
    assert soak_report.main([str(damaged)]) == 1
    assert "cannot read the soak record" in capsys.readouterr().out

    assert soak_report.main([str(tmp_path)]) == 1
    assert "no soak record at" in capsys.readouterr().out
