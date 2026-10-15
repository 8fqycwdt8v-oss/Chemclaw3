"""The restore drill: its pure parts, and one run of it against real containers.

The pure parts (what a report means, which instant a restore stops at, what a mismatch is) are
tested without Docker. `test_the_drill_restores_a_backup_to_its_marker` runs the whole drill and
skips, named and counted by `tests/conftest.py`, where no Docker daemon answers.
"""

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from chemclaw.cli import restore_drill as drill
from chemclaw.cli import restore_drill_db as db
from chemclaw.cli.restore_drill_db import Check, Snapshot
from chemclaw.kg.note import _CALC_REF

#: The head of the skip reason an absent Docker daemon produces, counted by `tests/conftest.py`.
DOCKER_SKIP = "Docker unavailable"

_COMPOSE = Path(__file__).resolve().parents[1] / "infra" / "docker-compose.yml"


def _snapshot(**changes: object) -> Snapshot:
    """A small consistent snapshot, with `changes` applied."""
    base: dict[str, object] = {
        "counts": {"session_messages": 3, "audit_events": 2},
        "ledger": [("000_schema_migrations.sql", "aa"), ("001_calculation_results.sql", "bb")],
        "indexes": [
            (
                "molecule_fingerprints",
                "molecule_fingerprints_jaccard_idx",
                "CREATE INDEX x USING hnsw",
            ),
            ("audit_events", "audit_events_ts_idx", "CREATE INDEX y USING btree (ts)"),
        ],
        "invalid_indexes": [],
        "grants": [
            ("audit_events", "INSERT", "chemclaw_app"),
            ("audit_events", "SELECT", "chemclaw_app"),
        ],
        "vector_version": "0.8.0",
        "markers": [db.MARKER_AT_TARGET],
    }
    base.update(changes)
    return Snapshot(**base)  # type: ignore[arg-type]


def _statuses(checks: list[Check]) -> dict[str, str]:
    return {check.name: check.status for check in checks}


def test_the_drill_uses_the_images_the_compose_file_runs() -> None:
    """The drill proves the stack developers run; a bumped image there is a bump here."""
    services = yaml.safe_load(_COMPOSE.read_text())["services"]
    assert drill.PG_IMAGE == services["postgres"]["image"]
    assert drill.TEMPORAL_IMAGE == services["temporal"]["image"]


def test_a_published_port_is_read_off_docker_port() -> None:
    assert drill.parse_published_port("127.0.0.1:32768\n") == 32768
    assert drill.parse_published_port("0.0.0.0:49153\n[::]:49153\n") == 49153
    with pytest.raises(drill.DrillError, match="cannot read a published port"):
        drill.parse_published_port("")


def test_the_recovery_target_is_an_explicit_utc_instant_with_microseconds() -> None:
    """A local-offset datetime is converted: the server parses the target in its own zone."""
    plus_two = timezone(timedelta(hours=2))
    instant = datetime(2026, 10, 14, 12, 30, 5, 123456, tzinfo=plus_two)
    assert drill.format_recovery_target(instant) == "2026-10-14 10:30:05.123456+00"
    settings = drill.recovery_settings(instant, "/archive")
    assert settings[0] == "restore_command = 'cp /archive/%f %p'"
    assert settings[1] == "recovery_target_time = '2026-10-14 10:30:05.123456+00'"
    assert "recovery_target_action = 'promote'" in settings


def test_a_report_is_ok_only_with_every_required_check_and_no_failure() -> None:
    names = sorted(drill.REQUIRED_CHECKS)
    report = drill.Report("t", {})
    report.checks = [db.passed(n) for n in names]
    assert report.ok
    # A named skip is a report line, not a failure.
    report.checks[0] = db.skipped(names[0], "no kg tables yet")
    assert report.ok
    assert report.as_dict()["skipped"] == [{"name": names[0], "reason": "no kg tables yet"}]
    report.checks[1] = db.failed(names[1], "differs")
    assert not report.ok
    # A check that never ran proves nothing about what it checks: missing is not ok.
    report.checks = [db.passed(n) for n in names[1:]]
    assert report.missing() == [names[0]]
    assert not report.ok
    assert report.as_dict()["missing_checks"] == [names[0]]
    # And a drill that could not finish is not ok whatever it had checked so far.
    report.checks = [db.passed(n) for n in names]
    report.error = "a container died"
    assert not report.ok


def test_a_restore_that_stops_at_the_target_holds_its_marker_and_nothing_after() -> None:
    assert db.marker_check([db.MARKER_AT_TARGET]).status == "pass"
    early = db.marker_check([])
    assert early.status == "fail" and "stopped early" in early.detail
    late = db.marker_check([db.MARKER_AT_TARGET, db.MARKER_AFTER_TARGET])
    assert late.status == "fail" and "past the target" in late.detail


def test_a_restore_that_ignored_the_target_is_visible_table_by_table() -> None:
    primary = dict.fromkeys(db.POST_TARGET_TABLES, 9)
    restored = dict(primary, session_messages=3)
    check = db.post_target_check(restored, primary)
    assert check.status == "fail"
    assert "audit_events" in check.detail and "session_messages" not in check.detail
    assert db.post_target_check(dict.fromkeys(db.POST_TARGET_TABLES, 3), primary).status == "pass"


def test_identical_snapshots_pass_every_structural_check() -> None:
    checks = db.compare_snapshots(_snapshot(), _snapshot())
    assert set(_statuses(checks).values()) == {"pass"}
    assert {c.name for c in checks} == {
        "row_counts",
        "schema_migrations",
        "indexes",
        "pgvector",
        "grants",
    }


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"counts": {"session_messages": 2, "audit_events": 2}}, "row_counts"),
        ({"counts": {"session_messages": 3}}, "row_counts"),
        ({"ledger": [("000_schema_migrations.sql", "aa")]}, "schema_migrations"),
        (
            {
                "ledger": [
                    ("000_schema_migrations.sql", "aa"),
                    ("001_calculation_results.sql", "XX"),
                ]
            },
            "schema_migrations",
        ),
        (
            {
                "indexes": [
                    ("audit_events", "audit_events_ts_idx", "CREATE INDEX y USING btree (ts)")
                ]
            },
            "indexes",
        ),
        ({"invalid_indexes": ["audit_events_ts_idx"]}, "indexes"),
        ({"vector_version": None}, "pgvector"),
        ({"vector_version": "0.7.0"}, "pgvector"),
        ({"grants": [("audit_events", "INSERT", "chemclaw_app")]}, "grants"),
        ({"grants": []}, "grants"),
    ],
)
def test_each_way_a_restore_can_differ_fails_its_own_named_check(
    changes: dict[str, object], failing: str
) -> None:
    checks = db.compare_snapshots(_snapshot(), _snapshot(**changes))
    assert _statuses(checks)[failing] == "fail"


def test_a_count_difference_names_the_table_and_both_numbers() -> None:
    lines = db.diff_counts({"a": 3, "b": 1}, {"a": 2, "c": 1})
    assert lines == [
        "a: expected 3, restored 2",
        "b: missing from the restored database",
        "c: not in the primary at the target",
    ]


def test_the_fixture_key_has_the_shape_a_note_may_cite() -> None:
    """`kg-validate` only resolves a `calc_refs` entry of this shape against the cache."""
    assert _CALC_REF.fullmatch(db.calculation_key("a-0"))
    assert db.calculation_key("a-0") != db.calculation_key("c-0")


def test_the_make_target_and_the_readme_name_the_drill() -> None:
    root = Path(__file__).resolve().parents[1]
    makefile = (root / "Makefile").read_text()
    assert re.search(r"^restore-drill:", makefile, flags=re.MULTILINE)
    readme = (root / "src" / "chemclaw" / "cli" / "README.md").read_text()
    assert "restore_drill" in readme


def _labelled(run_id: str) -> str:
    """The Docker resources still labelled for `run_id` (containers, volumes, networks)."""
    selector = f"label={drill.LABEL}={run_id}"
    out = ""
    for noun in (["ps", "-aq"], ["volume", "ls", "-q"], ["network", "ls", "-q"]):
        out += drill.docker(*noun, "--filter", selector, check=False)
    return out.strip()


@pytest.mark.timeout(900)
def test_the_drill_restores_a_backup_to_its_marker() -> None:
    """The whole drill, against a real Postgres and a real Temporal, in containers of its own.

    Expected to pass on a healthy stack: every required check present, none failed, the only
    permitted skip the knowledge tables of a schema that has none yet, and nothing of the run left
    behind.
    """
    reason = drill.docker_available()
    if reason is not None:
        pytest.skip(f"{DOCKER_SKIP}: {reason}")
    report = drill.run_drill()
    try:
        assert not report.error, report.error
        failed_checks = [c.as_dict() for c in report.checks if c.status == "fail"]
        assert not failed_checks, failed_checks
        assert report.missing() == []
        assert {c.name for c in report.checks if c.status == "skip"} <= {"kg_tables"}
        assert report.ok
        assert report.facts["restore_seconds"] > 0
    finally:
        left = _labelled(report.run_id)
        assert left == "", f"the drill left resources behind: {left}"


def test_a_missing_docker_cli_is_an_error_report_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_docker(*_args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", no_docker)
    report = drill.run_drill()
    assert report.error.startswith("Docker unavailable")
    assert not report.ok
