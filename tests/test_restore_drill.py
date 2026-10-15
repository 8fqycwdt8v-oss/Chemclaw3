"""The restore drill: its pure parts, and one run of it against real containers.

The pure parts (what a report means, which instant a restore stops at, what a mismatch is) are
tested without Docker. `test_the_drill_restores_a_backup_to_its_marker` runs the whole drill and
skips, named and counted by `tests/conftest.py`, where no Docker daemon answers.
"""

import re
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from chemclaw.cli import restore_drill as drill
from chemclaw.core import restore_check as db
from chemclaw.core.config import settings
from chemclaw.core.restore_check import Check, Snapshot
from chemclaw.kg.note import _CALC_REF
from chemclaw.science.calc.store import CalculationKey

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
    # The kernel module spells the key out rather than import `science`; this is the one place the
    # two are compared.
    real = CalculationKey(
        calc_type="drill", calc_version="v1", input_hash="in-a-0", params_hash="pa-a-0"
    )
    assert real.as_str() == db.calculation_key("a-0")


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
    permitted skip none (the schema defines the knowledge tables), and nothing of the run left
    behind. An image that is neither local nor pullable (a registry rate limit, no network) skips
    under the same counted marker: it says nothing about the procedure. Every other failure fails.
    """
    reason = drill.docker_available()
    if reason is not None:
        pytest.skip(f"{DOCKER_SKIP}: {reason}")
    report = drill.run_drill()
    if report.error.startswith("Image unavailable"):
        pytest.skip(f"{DOCKER_SKIP}: {report.error}")
    try:
        assert not report.error, report.error
        failed_checks = [c.as_dict() for c in report.checks if c.status == "fail"]
        assert not failed_checks, failed_checks
        assert report.missing() == []
        assert [c.name for c in report.checks if c.status == "skip"] == []
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


def test_absent_knowledge_tables_fail_where_the_migrations_define_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A skipped `kg_tables` was a pass for a restore that lost the notes; now it is a failure."""
    monkeypatch.setattr(db, "knowledge_counts", lambda _dsn: {})
    monkeypatch.setattr(
        drill, "_validate_kg", lambda corpus, _dsn: 0 if "good" in str(corpus) else 1
    )
    (tmp_path / "128_kg_notes.sql").write_text("CREATE TABLE kg_notes (id text);")
    monkeypatch.setattr(settings, "sql_migrations_dir", str(tmp_path))
    assert drill.knowledge_expected()
    table_check, validated = drill.kg_checks("dsn", "before", "after")
    assert table_check.status == "fail" and "no kg_* table" in table_check.detail
    assert validated.status == "pass"

    (tmp_path / "128_kg_notes.sql").write_text("CREATE TABLE other (id text);")
    assert not drill.knowledge_expected()
    assert drill.kg_checks("dsn", "before", "after")[0].status == "skip"


def test_the_validator_must_refuse_the_post_target_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "knowledge_counts", lambda _dsn: {"kg_notes": 3})
    monkeypatch.setattr(drill, "_validate_kg", lambda _corpus, _dsn: 0)
    _, validated = drill.kg_checks("dsn", "before", "after")
    assert validated.status == "fail" and "not restored to the target" in validated.detail


def test_cleanup_names_what_it_could_not_remove_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: str, **_kwargs: object) -> str:
        raise drill.DrillError("daemon went away")

    monkeypatch.setattr(drill, "docker", broken)
    problems = drill.remove_run("abc123")
    assert problems and "daemon went away" in problems[0]


def test_a_failed_cleanup_is_reported_without_replacing_the_runs_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object) -> None:
        raise drill.DrillError("the primary died")

    monkeypatch.setattr(drill, "docker_available", lambda: None)
    monkeypatch.setattr(drill, "ensure_images", lambda *_images: None)
    monkeypatch.setattr(drill, "_drill", boom)
    monkeypatch.setattr(drill, "remove_run", lambda _id: ["volume busy"])
    report = drill.run_drill()
    assert report.error == "the primary died"
    assert [(c.name, c.status) for c in report.checks] == [("cleanup", "fail")]
    assert not report.ok

    def explodes(_id: str) -> list[str]:
        raise OSError("socket closed")

    monkeypatch.setattr(drill, "remove_run", explodes)
    assert drill.run_drill().error == "the primary died"


def test_a_signal_while_cleaning_up_does_not_abandon_the_cleanup() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with drill.uninterruptible():
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGINT)
    assert signal.getsignal(signal.SIGTERM) == before


def test_an_image_that_cannot_be_pulled_is_named_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*args: str, **_kwargs: object) -> str:
        raise drill.DrillError("toomanyrequests: rate limit")

    monkeypatch.setattr(drill, "_succeeds", lambda *_args: False)
    monkeypatch.setattr(drill, "docker", refused)
    with pytest.raises(drill.ImageUnavailable, match="rate limit"):
        drill.ensure_images("pgvector/pgvector:pg16")
    monkeypatch.setattr(drill, "docker_available", lambda: None)
    assert drill.run_drill().error.startswith("Image unavailable")


def test_the_restore_time_is_checked_against_a_stated_rto_and_labelled_as_a_fixture() -> None:
    report = drill.Report("t", {}, facts={"base_backup_bytes": 53_000_000})
    ok = drill._restore_time_check(1.4, 600, report)
    over = drill._restore_time_check(700.0, 600, report)
    assert ok.status == "pass" and over.status == "fail"
    assert "53 MB fixture" in over.detail and "not on the site's data" in over.detail
    assert "restore_time" not in drill.REQUIRED_CHECKS
