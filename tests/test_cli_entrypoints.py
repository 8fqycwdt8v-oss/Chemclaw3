"""Entry points that mutate something must answer their command line before they do it.

`tests/test_validator_entrypoints.py` holds this for the read-only gates; this file holds it for
the `chemclaw.cli` commands that reach a broker or a database, plus the reporting command run
beside them: `--help` must not apply Schedules, and a flag or a blank id must not be looked up as
a session id. `_apply` is stubbed to fail if reached, so "parsed instead of applied" is asserted.
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

    Unlike its preview-by-default siblings, this runs as a container command with no arguments
    (`schedules-job.yaml`); previewing by default would make every Schedule Job a green no-op.
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
    """The audit reconstruction refuses a blank session id.

    Rows written before the correlation id existed carry `session_id = ''`, so `explain ""` (an
    unset shell variable) would print another actor's audit rows.
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
    """A damaged soak record is reported rather than raised.

    `infra/live/soak.sh` appends per round, so a killed run leaves a half-written line; a directory
    argument is reported the same way.
    """
    from chemclaw.cli import soak_report

    damaged = tmp_path / "record.jsonl"
    damaged.write_text('{"round": 1, "api": {"rss_kb": 1}}\n{"round": 2, "ap\n', encoding="utf-8")
    assert soak_report.main([str(damaged)]) == 1
    assert "cannot read the soak record" in capsys.readouterr().out

    assert soak_report.main([str(tmp_path)]) == 1
    assert "no soak record at" in capsys.readouterr().out
