"""`make restore-drill`: back up a throwaway Postgres, restore it to a marked instant, check it.

Proves the procedure `docs/guides/runbook.md` § "Backup, restore and PITR" describes, on the stack
`infra/docker-compose.yml` runs: a pgvector Postgres 16 archiving WAL to a directory, the schema
and a fixture loaded through the real migrations, a base backup, more writes, a marker and its
timestamp, then writes the restore must NOT contain (including the conversation table emptied). A
second container is built from the base backup and replayed to the marker with
`restore_command` + `recovery_target_time`; Temporal's own databases ride the same cluster and are
restored with it.

It uses its own containers, network, volumes and ephemeral loopback ports, every one labelled with
this run's id and removed on every exit path; it never touches the dev database. Progress goes to
stderr and one JSON report to stdout. Exit 0 = every check passed, 1 = a check failed, 2 = the
drill could not run (no Docker, a container died).
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chemclaw.core import restore_check as db
from chemclaw.core.config import settings
from chemclaw.core.restore_check import Check, failed, passed, skipped

#: Same images as `infra/docker-compose.yml`; `tests/test_restore_drill.py` holds them equal.
PG_IMAGE = "pgvector/pgvector:pg16"
TEMPORAL_IMAGE = "temporalio/auto-setup:1.25.2"

#: Every resource of every run carries this label (value: the run id), which is how cleanup finds
#: what a killed run left behind.
LABEL = "chemclaw.restore-drill"

_DB_USER = "chemclaw"
_DB_PASSWORD = "chemclaw"
_DB_NAME = "chemclaw"
_APP_PASSWORD = "restore-drill-app"
_PG_UID = "999"
_WORKFLOW_TYPE = "RestoreDrillWorkflow"

#: Rows per wave: before the backup, between backup and target, after the target.
_WAVES = (("a", 6, 11), ("b", 4, 22), ("c", 3, 33))

#: Gap between the target instant and the first write after it, so no commit shares the instant.
_TARGET_GAP_SECONDS = 1.5
_READY_SECONDS = 90.0
_RECOVERY_SECONDS = 180.0
_TEMPORAL_READY_SECONDS = 120.0
_ARCHIVE_SECONDS = 60.0
_TEMPORAL_WORKFLOWS = 5

#: Checks a finished report must name (as pass, fail or skip), so none can vanish unnoticed.
REQUIRED_CHECKS = frozenset(
    {
        "wal_archiving",
        "marker",
        "row_counts",
        "post_target_rows_excluded",
        "schema_migrations",
        "indexes",
        "pgvector",
        "pgvector_query",
        "grants",
        "app_role",
        "promoted",
        "listen_notify",
        "advisory_lock",
        "kg_tables",
        "kg_validate",
        "temporal",
    }
)


class DrillError(RuntimeError):
    """The drill could not run (as opposed to a check failing)."""


class ImageUnavailable(DrillError):
    """An image is neither local nor pullable: the network's fault, not the procedure's."""


@dataclass
class Report:
    """The machine-readable result: checks, timings and the facts that make them reproducible."""

    run_id: str
    images: dict[str, str]
    checks: list[Check] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def add(self, *checks: Check) -> None:
        """Record checks, echoing each to stderr as it lands."""
        for check in checks:
            self.checks.append(check)
            _say(f"  [{check.status:4}] {check.name}: {check.detail}")

    def missing(self) -> list[str]:
        """Required checks no one reported; a drill that skipped them proved nothing about them."""
        return sorted(REQUIRED_CHECKS - {check.name for check in self.checks})

    @property
    def ok(self) -> bool:
        """No failed check, no required check missing, and the drill ran to its end."""
        return (
            not self.error and not self.missing() and all(c.status != "fail" for c in self.checks)
        )

    def as_dict(self) -> dict[str, Any]:
        """The JSON report."""
        return {
            "drill": "restore",
            "ok": self.ok,
            "run_id": self.run_id,
            "images": self.images,
            "error": self.error,
            "missing_checks": self.missing(),
            "skipped": [
                {"name": c.name, "reason": c.detail} for c in self.checks if c.status == "skip"
            ],
            "timings_seconds": {k: round(v, 2) for k, v in self.timings.items()},
            "facts": self.facts,
            "checks": [c.as_dict() for c in self.checks],
        }


def _say(message: str) -> None:
    """Progress to stderr, so stdout stays one JSON document."""
    print(message, file=sys.stderr, flush=True)


def parse_published_port(docker_port_output: str) -> int:
    """The host port out of `docker port` output such as `127.0.0.1:32768`.

    Raises:
        DrillError: When no port can be read, naming the output.
    """
    match = re.search(
        r":(\d+)\s*$",
        docker_port_output.strip().splitlines()[0] if docker_port_output.strip() else "",
    )
    if match is None:
        raise DrillError(f"cannot read a published port from {docker_port_output!r}")
    return int(match.group(1))


def format_recovery_target(instant: datetime) -> str:
    """A `recovery_target_time` literal for `instant`: microseconds, explicit UTC offset."""
    return instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00")


def recovery_settings(target: datetime, archive_dir: str) -> list[str]:
    """The `postgresql.auto.conf` lines that make a copied base backup replay to `target`.

    `promote` ends recovery at the target so the restored cluster is writable; the inclusive
    default keeps the commit at exactly the target instant.
    """
    return [
        f"restore_command = 'cp {archive_dir}/%f %p'",
        f"recovery_target_time = '{format_recovery_target(target)}'",
        "recovery_target_action = 'promote'",
    ]


def dsn(port: int, user: str = _DB_USER, password: str = _DB_PASSWORD) -> str:
    """A loopback DSN for a published port."""
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{_DB_NAME}"


def docker(*args: str, timeout: float = 300.0, check: bool = True, merge: bool = False) -> str:
    """Run `docker <args>`; stdout on success (stdout and stderr together with `merge`).

    Raises:
        DrillError: When the command fails (and `check`), carrying its stderr.
    """
    try:
        done = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise DrillError("the docker CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise DrillError(f"`docker {' '.join(args[:3])}` timed out after {timeout:.0f} s") from exc
    if check and done.returncode != 0:
        raise DrillError(f"`docker {' '.join(args[:3])}` failed: {done.stderr.strip()[-600:]}")
    return done.stdout + done.stderr if merge else done.stdout


def _succeeds(*args: str) -> bool:
    """Whether `docker <args>` exits 0."""
    try:
        done = subprocess.run(["docker", *args], capture_output=True, check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def ensure_images(*images: str) -> None:
    """Pull what is not already local, so a registry refusing us is named before anything starts.

    Raises:
        ImageUnavailable: When an image is not local and cannot be pulled (rate limit, no network).
    """
    for image in images:
        if _succeeds("image", "inspect", image):
            continue
        _say(f"== pulling {image}")
        try:
            docker("pull", image, timeout=900.0)
        except DrillError as exc:
            raise ImageUnavailable(f"{image} is not local and could not be pulled: {exc}") from exc


def docker_available() -> str | None:
    """None when the daemon answers, else why not."""
    try:
        docker("info", "--format", "{{.ServerVersion}}", timeout=20.0)
    except DrillError as exc:
        return str(exc)
    return None


class Stack:
    """The containers, volumes and network of one run, all labelled with its id."""

    def __init__(self, run_id: str, pg_image: str, temporal_image: str) -> None:
        """Name the run's resources; nothing is created until `create`."""
        self.run_id = run_id
        self.pg_image = pg_image
        self.temporal_image = temporal_image
        self.network = f"restore-drill-{run_id}"
        self.archive = f"{self.network}-archive"
        self.backup = f"{self.network}-backup"
        self.restored = f"{self.network}-restored"

    def container(self, role: str) -> str:
        """The container name for `role`."""
        return f"{self.network}-{role}"

    def create(self) -> None:
        """The network and the volumes WAL and the base backup travel through."""
        label = f"{LABEL}={self.run_id}"
        docker("network", "create", "--label", label, self.network)
        for volume in (self.archive, self.backup, self.restored):
            docker("volume", "create", "--label", label, volume)
        # A fresh volume is root-owned; the postgres user (uid 999) must write both.
        docker(
            "run", "--rm", "--user", "root", "--label", label,
            "-v", f"{self.archive}:/archive", "-v", f"{self.backup}:/backup",
            self.pg_image, "chown", f"{_PG_UID}:{_PG_UID}", "/archive", "/backup",
        )  # fmt: skip

    def _run(self, role: str, env: dict[str, str], port: int, tail: Sequence[str]) -> int:
        """`docker run -d` one labelled container on the run network; its published port."""
        name = self.container(role)
        args = ["run", "-d", "--name", name, "--network", self.network]
        args += ["--label", f"{LABEL}={self.run_id}", "-p", f"127.0.0.1::{port}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        docker(*args, *tail)
        return parse_published_port(docker("port", name, f"{port}/tcp"))

    def run_postgres(self, role: str, mounts: Sequence[str], command: Sequence[str] = ()) -> int:
        """Start a Postgres container with `mounts` (`-v` arguments); its published port."""
        env = {
            "POSTGRES_USER": _DB_USER,
            "POSTGRES_PASSWORD": _DB_PASSWORD,
            "POSTGRES_DB": _DB_NAME,
        }
        return self._run(role, env, 5432, [*mounts, self.pg_image, *command])

    def run_temporal(self, role: str, postgres_role: str) -> int:
        """Start Temporal against `postgres_role`'s server; its published frontend port."""
        env = {
            "DB": "postgres12",
            "DB_PORT": "5432",
            "POSTGRES_USER": _DB_USER,
            "POSTGRES_PWD": _DB_PASSWORD,
            "POSTGRES_SEEDS": self.container(postgres_role),
        }
        return self._run(role, env, 7233, [self.temporal_image])

    def logs(self, role: str, tail: int = 25) -> str:
        """The last lines a container logged, for an error message."""
        return docker("logs", "--tail", str(tail), self.container(role), check=False, merge=True)

    def alive(self, role: str) -> bool:
        """Whether the container is still running."""
        out = docker("inspect", "-f", "{{.State.Running}}", self.container(role), check=False)
        return out.strip() == "true"


def remove_run(run_id: str | None) -> list[str]:
    """Remove every container, volume and network labelled for `run_id` (or for any run).

    Never raises: a daemon that errors halfway must not mask the run's own result, so what could not
    be removed is returned for the report to name.
    """
    selector = f"{LABEL}={run_id}" if run_id else LABEL
    problems: list[str] = []

    def attempt(*args: str) -> str:
        try:
            return docker(*args, check=True)
        except DrillError as exc:
            problems.append(str(exc))
            return ""

    containers = attempt("ps", "-aq", "--filter", f"label={selector}").split()
    if containers:
        attempt("rm", "-f", "-v", *containers)
    volumes = attempt("volume", "ls", "-q", "--filter", f"label={selector}").split()
    if volumes:
        attempt("volume", "rm", "-f", *volumes)
    networks = attempt("network", "ls", "-q", "--filter", f"label={selector}").split()
    if networks:
        attempt("network", "rm", *networks)
    return problems


#: Set by the first SIGINT or SIGTERM. The drill stops at its next step boundary or wait, as an
#: ordinary failure, so its cleanup runs on the normal path and no exception lands at an arbitrary
#: line; only a second signal interrupts at once.
_stop_signal: int | None = None


def _on_signal(signum: int, _frame: object) -> None:
    """First signal: ask the drill to stop. Second: the operator insists, so raise now."""
    global _stop_signal
    if _stop_signal is not None:
        raise KeyboardInterrupt(f"signal {signum}")
    _stop_signal = signum


def check_stopped() -> None:
    """Raise `DrillError` once a stop was requested.

    Raises:
        DrillError: Naming the signal.
    """
    if _stop_signal is not None:
        raise DrillError(f"stopped by signal {_stop_signal}")


@contextmanager
def phase(report: Report, name: str) -> Iterator[None]:
    """Time a named phase into the report."""
    check_stopped()
    _say(f"== {name}")
    started = time.monotonic()
    try:
        yield
    finally:
        report.timings[name] = time.monotonic() - started


def wait_for(
    probe: Callable[[], bool], seconds: float, what: str, alive: Callable[[], bool] | None = None
) -> None:
    """Poll `probe` until it holds.

    Raises:
        DrillError: On timeout, or as soon as `alive` says the thing being waited on has died.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_stopped()
        try:
            if probe():
                return
        except OSError:
            pass
        if alive is not None and not alive():
            raise DrillError(f"{what}: the container exited")
        time.sleep(0.5)
    raise DrillError(f"{what}: not ready after {seconds:.0f} s")


def wait_for_postgres(port: int, stack: Stack, role: str, *, recovered: bool = False) -> None:
    """Until `role` accepts connections (and, if `recovered`, has left recovery)."""
    seconds = _RECOVERY_SECONDS if recovered else _READY_SECONDS
    try:
        wait_for(
            lambda: db.is_ready(dsn(port), recovered=recovered),
            seconds,
            f"{role} postgres",
            lambda: stack.alive(role),
        )
    except DrillError as exc:
        raise DrillError(f"{exc}\n{stack.logs(role)}") from exc


def run_module(module: str, target_dsn: str) -> None:
    """Run `python -m <module>` against `target_dsn` as `make db-migrate` and the migrate Job do."""
    env = {
        **os.environ,
        "CHEMCLAW_POSTGRES_DSN": target_dsn,
        "CHEMCLAW_POSTGRES_MIGRATION_DSN": target_dsn,
    }
    env.pop("CHEMCLAW_SESSION_STORE_DSN", None)
    done = subprocess.run(
        [sys.executable, "-m", module],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if done.returncode != 0:
        raise DrillError(f"python -m {module} failed:\n{(done.stdout + done.stderr)[-800:]}")


async def _setup_checkpointer(target_dsn: str) -> None:
    """Create the LangGraph checkpointer's tables, as the serving process does at first use."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(target_dsn) as saver:
        await saver.setup()


def bootstrap_schema(target_dsn: str) -> None:
    """The migrate Job's steps, plus the split runtime role and the checkpointer tables."""
    run_module("chemclaw.core.migrate", target_dsn)
    run_module("chemclaw.agent.store_setup", target_dsn)
    asyncio.run(_setup_checkpointer(target_dsn))
    db.prepare(target_dsn, _APP_PASSWORD)
    run_module("chemclaw.core.grants", target_dsn)


def wait_for_archive(stack: Stack, role: str, primary_dsn: str) -> Check:
    """Switch the WAL segment and wait until the archive holds everything written so far."""
    segment = db.switch_wal(primary_dsn)

    def archived() -> bool:
        done = subprocess.run(
            ["docker", "exec", stack.container(role), "test", "-f", f"/archive/{segment}"],
            capture_output=True, check=False,
        )  # fmt: skip
        return done.returncode == 0

    try:
        wait_for(archived, _ARCHIVE_SECONDS, f"WAL segment {segment} in the archive")
    except DrillError as exc:
        return failed("wal_archiving", str(exc))
    archived_count, failed_count = db.archiver_stats(primary_dsn)
    if failed_count != 0:
        return failed("wal_archiving", f"{failed_count} archive attempts failed")
    return passed(
        "wal_archiving", f"{archived_count} segments archived, 0 failures; {segment} present"
    )


def directory_bytes(stack: Stack, role: str, path: str) -> int:
    """Bytes under `path` inside `role`'s container."""
    out = docker("exec", stack.container(role), "du", "-sb", path, check=False)
    return int(out.split()[0]) if out.split() else 0


def take_base_backup(stack: Stack, role: str) -> int:
    """`pg_basebackup` into the backup volume without WAL (it is in the archive); its size."""
    docker(
        "exec", "-u", "postgres", stack.container(role), "pg_basebackup", "-U", _DB_USER,
        "-D", "/backup/base", "-Fp", "-X", "none", "-c", "fast",
    )  # fmt: skip
    return directory_bytes(stack, role, "/backup/base")


def restore_cluster(stack: Stack, target: datetime) -> int:
    """Build a second cluster from the base backup, replay the archive to `target`; its port."""
    script = (
        'set -e; d=/var/lib/postgresql/data; cp -a /backup/base/. "$d"/; '
        'chown -R postgres:postgres "$d"; chmod 700 "$d"; touch "$d"/recovery.signal; '
        'printf \'%s\\n\' "$RECOVERY" >> "$d"/postgresql.auto.conf'
    )
    docker(
        "run", "--rm", "--user", "root", "--label", f"{LABEL}={stack.run_id}",
        "-e", "RECOVERY=" + "\n".join(recovery_settings(target, "/archive")),
        "-v", f"{stack.backup}:/backup:ro", "-v", f"{stack.restored}:/var/lib/postgresql/data",
        stack.pg_image, "bash", "-c", script,
    )  # fmt: skip
    return stack.run_postgres(
        "restored",
        ["-v", f"{stack.restored}:/var/lib/postgresql/data", "-v", f"{stack.archive}:/archive:ro"],
    )


def _write_note(root: Path, note_id: str, calc_key: str) -> None:
    """A `job-result` note whose `calc_refs` cites one calculation, as the agent records them."""
    directory = root / "job-result"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: job-result\ncreated_by: agent\n"
        f"calc_refs:\n  - {calc_key}\n---\n\nA cited calculation.\n",
        encoding="utf-8",
    )


def _validate_kg(corpus: Path, target_dsn: str) -> int:
    """`kg-validate` over `corpus` against `target_dsn`'s calculation cache; its exit code."""
    env = {**os.environ, "CHEMCLAW_POSTGRES_DSN": target_dsn}
    env.pop("CHEMCLAW_SESSION_STORE_DSN", None)
    done = subprocess.run(
        [sys.executable, "-m", "chemclaw.cli.validate_kg", str(corpus)],
        env=env, capture_output=True, text=True, check=False, timeout=300,
    )  # fmt: skip
    return done.returncode


def knowledge_expected() -> bool:
    """Whether the migrations this drill applies define the knowledge tables."""
    return any(
        "kg_notes" in path.read_text(encoding="utf-8")
        for path in Path(settings.sql_migrations_dir).glob("*.sql")
    )


def kg_checks(target_dsn: str, before: str, after: str) -> list[Check]:
    """`kg-validate` against the restored database, with a negative control; and the `kg_*` tables.

    The corpus cites a calculation written before the target (must resolve) and, in a second
    corpus, one written after it (must NOT resolve: it proves the validator really asks the
    restored database, and that the restore stopped where it was told to). Absent tables are a skip
    only for a schema that defines none; where the migrations define them it is a failure.
    """
    counts = db.knowledge_counts(target_dsn)
    if counts:
        empty = [t for t, n in counts.items() if n == 0 and t != "kg_edges"]
        table_check = (
            failed("kg_tables", f"restored but empty: {empty}")
            if empty
            else passed("kg_tables", f"restored: {counts}")
        )
    elif knowledge_expected():
        table_check = failed(
            "kg_tables",
            "the migrations define kg_notes but the restored database has no kg_* table",
        )
    else:
        table_check = skipped("kg_tables", "this schema defines no kg_* tables")
    with tempfile.TemporaryDirectory() as tmp:
        good, bad = Path(tmp) / "good", Path(tmp) / "bad"
        _write_note(good, "drill-note-cited", before)
        _write_note(bad, "drill-note-lost", after)
        resolves, lost = _validate_kg(good, target_dsn), _validate_kg(bad, target_dsn)
    if resolves != 0:
        validated = failed(
            "kg_validate",
            f"a citation of a pre-target calculation did not resolve (exit {resolves})",
        )
    elif lost == 0:
        validated = failed(
            "kg_validate",
            "a citation of a post-target calculation resolved: not restored to the target",
        )
    else:
        validated = passed(
            "kg_validate", "pre-target citation resolves, post-target citation does not"
        )
    return [table_check, validated]


async def _temporal_client(port: int) -> Any:
    """Connect to a Temporal frontend, retrying while it finishes starting."""
    from temporalio.client import Client

    deadline = time.monotonic() + _TEMPORAL_READY_SECONDS
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = await Client.connect(f"127.0.0.1:{port}")
            await client.count_workflows(f'WorkflowType = "{_WORKFLOW_TYPE}"')
            return client
        except Exception as exc:
            last = exc
            await asyncio.sleep(1.0)
    raise DrillError(f"Temporal frontend on port {port} not ready: {last}")


async def _count_workflows(client: Any, expected: int | None, seconds: float) -> int:
    """The drill workflows visible, waiting for `expected` (visibility is written lazily)."""
    deadline = time.monotonic() + seconds
    count = 0
    while True:
        count = (await client.count_workflows(f'WorkflowType = "{_WORKFLOW_TYPE}"')).count
        if expected is None or count == expected or time.monotonic() > deadline:
            return int(count)
        await asyncio.sleep(1.0)


def temporal_start(port: int, first: int, count: int) -> None:
    """Start `count` workflows (no worker polls: they stay open) and wait until they are visible."""

    async def go() -> None:
        client = await _temporal_client(port)
        for i in range(first, first + count):
            await client.start_workflow(
                _WORKFLOW_TYPE, id=f"restore-drill-{i}", task_queue="restore-drill"
            )
        seen = await _count_workflows(client, first + count, 60.0)
        if seen != first + count:
            raise DrillError(f"Temporal lists {seen} drill workflows, expected {first + count}")

    asyncio.run(go())


def temporal_check(port: int, expected: int) -> Check:
    """The restored Temporal answers, has its namespace and lists the workflows of the target."""

    async def go() -> Check:
        from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

        client = await _temporal_client(port)
        namespace = await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace="default")
        )
        seen = await _count_workflows(client, expected, 60.0)
        if seen != expected:
            return failed(
                "temporal", f"{seen} workflows listed, {expected} were running at the target"
            )
        return passed(
            "temporal",
            f"namespace '{namespace.namespace_info.name}' restored; {seen} workflows at the target",
        )

    return asyncio.run(go())


def run_drill(
    pg_image: str = PG_IMAGE,
    temporal_image: str = TEMPORAL_IMAGE,
    with_temporal: bool = True,
    keep: bool = False,
    rto_seconds: float | None = None,
) -> Report:
    """Run the whole drill and return its report; every container is removed unless `keep`.

    With `rto_seconds`, the restore's wall time is also checked against it. The drill restores a
    fixture of a few megabytes, so that check measures the procedure, not a site's data volume.
    """
    run_id = secrets.token_hex(3)
    stack = Stack(run_id, pg_image, temporal_image)
    images = [pg_image, *([temporal_image] if with_temporal else [])]
    report = Report(
        run_id, {"postgres": pg_image, "temporal": temporal_image if with_temporal else "(not run)"}
    )
    started = time.monotonic()
    reason = docker_available()
    if reason is not None:
        report.error = f"Docker unavailable: {reason}"
        return report
    try:
        ensure_images(*images)
        _drill(stack, report, with_temporal, rto_seconds)
    except ImageUnavailable as exc:
        report.error = f"Image unavailable: {exc}"
    except DrillError as exc:
        report.error = str(exc)
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        report.timings["total"] = time.monotonic() - started
        if keep:
            _say(f"--keep: run {run_id} left in place; remove it with `--prune`")
        else:
            _clean_up(run_id, report)
    return report


def _clean_up(run_id: str, report: Report) -> None:
    """Remove the run's resources and name what stayed; finish even if a signal lands mid-way.

    `remove_run` is idempotent, so a pass cut short by a second signal is repeated, and the signal
    is re-raised once the resources are gone.
    """
    interrupted: KeyboardInterrupt | None = None
    leftovers: list[str] = []
    while True:
        try:
            leftovers = remove_run(run_id)
            break
        except KeyboardInterrupt as exc:
            interrupted = exc
        except Exception as exc:  # cleanup must never replace the run's own result
            leftovers = [f"{type(exc).__name__}: {exc}"]
            break
    if leftovers:
        report.add(failed("cleanup", f"could not remove: {'; '.join(leftovers)[:400]}"))
    if interrupted is not None:
        raise interrupted


def _drill(stack: Stack, report: Report, with_temporal: bool, rto_seconds: float | None) -> None:
    """The drill's steps, in order; checks land in `report` as they are made."""
    with phase(report, "start_primary"):
        stack.create()
        port = stack.run_postgres(
            "primary",
            ["-v", f"{stack.archive}:/archive", "-v", f"{stack.backup}:/backup"],
            ["postgres", "-c", "wal_level=replica", "-c", "archive_mode=on",
             "-c", "archive_command=test ! -f /archive/%f && cp %p /archive/%f"],
        )  # fmt: skip
        wait_for_postgres(port, stack, "primary")
    primary = dsn(port)
    with phase(report, "migrate_and_load"):
        bootstrap_schema(primary)
        tag, rows, seed = _WAVES[0]
        report.facts["knowledge_tables"] = db.load_wave(primary, tag, rows, seed)
    temporal_port = 0
    if with_temporal:
        with phase(report, "temporal_up"):
            temporal_port = stack.run_temporal("temporal", "primary")
            temporal_start(temporal_port, 0, _TEMPORAL_WORKFLOWS)
    with phase(report, "base_backup"):
        report.facts["base_backup_bytes"] = take_base_backup(stack, "primary")
    with phase(report, "writes_after_backup"):
        tag, rows, seed = _WAVES[1]
        db.load_wave(primary, tag, rows, seed)
        db.change_wave(primary, _WAVES[0][0])
        db.mark(primary, db.MARKER_AT_TARGET)
        expected = db.snapshot(primary)
        probe = f"{_WAVES[0][0]}-0"
        nearest = db.nearest_neighbour(primary, probe)
        target = db.clock_now(primary)
    report.facts["target_time"] = format_recovery_target(target)
    _say(f"== target instant {report.facts['target_time']}")
    time.sleep(_TARGET_GAP_SECONDS)
    with phase(report, "writes_after_target"):
        tag, rows, seed = _WAVES[2]
        db.load_wave(primary, tag, rows, seed)
        db.mark(primary, db.MARKER_AFTER_TARGET)
        db.empty_conversations(primary)
        if with_temporal:
            temporal_start(temporal_port, _TEMPORAL_WORKFLOWS, 1)
        final = db.snapshot(primary)
    report.add(wait_for_archive(stack, "primary", primary))
    report.facts["wal_archive_bytes"] = directory_bytes(stack, "primary", "/archive")
    if with_temporal:
        docker("stop", "-t", "30", stack.container("temporal"))
    docker("stop", "-t", "30", stack.container("primary"))
    with phase(report, "restore_to_target"):
        restored_port = restore_cluster(stack, target)
        wait_for_postgres(restored_port, stack, "restored", recovered=True)
    restored_dsn = dsn(restored_port)
    report.facts["restore_seconds"] = round(report.timings["restore_to_target"], 2)
    if rto_seconds is not None:
        report.add(_restore_time_check(report.facts["restore_seconds"], rto_seconds, report))
    with phase(report, "verify"):
        restored = db.snapshot(restored_dsn)
        report.add(db.marker_check(restored.markers), *db.compare_snapshots(expected, restored))
        report.add(db.post_target_check(restored.counts, final.counts))
        report.add(db.vector_query_check(restored_dsn, probe, nearest))
        report.add(db.notify_check(restored_dsn), db.advisory_lock_check(restored_dsn))
        report.add(db.app_role_check(dsn(restored_port, db.APP_ROLE, _APP_PASSWORD)))
        report.add(
            *kg_checks(
                restored_dsn,
                db.calculation_key(f"{_WAVES[0][0]}-0"),
                db.calculation_key(f"{_WAVES[2][0]}-0"),
            )
        )
        report.add(db.promotion_check(restored_dsn))
    if with_temporal:
        with phase(report, "verify_temporal"):
            temporal_port = stack.run_temporal("temporal-restored", "restored")
            report.add(temporal_check(temporal_port, _TEMPORAL_WORKFLOWS))
    else:
        report.add(
            skipped("temporal", "--no-temporal: Temporal's databases were not part of this run")
        )
    report.facts["rows_at_target"] = sum(expected.counts.values())


def _restore_time_check(seconds: float, rto_seconds: float, report: Report) -> Check:
    """The restore's wall time against a stated RTO, labelled with the size it was measured at."""
    size = int(report.facts.get("base_backup_bytes", 0)) // 1_000_000
    where = f"measured on a {size} MB fixture, not on the site's data"
    if seconds > rto_seconds:
        return failed("restore_time", f"{seconds} s exceeds {rto_seconds:g} s ({where})")
    return passed("restore_time", f"{seconds} s within {rto_seconds:g} s ({where})")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the drill (or `--prune`); print the JSON report; return the exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.restore_drill",
        description="Back up a throwaway Postgres, restore it to a marked instant, verify it.",
    )
    parser.add_argument(
        "--pg-image", default=PG_IMAGE, help="Postgres image (default: the compose file's)"
    )
    parser.add_argument(
        "--temporal-image",
        default=TEMPORAL_IMAGE,
        help="Temporal image (default: the compose file's)",
    )
    parser.add_argument(
        "--no-temporal", action="store_true", help="leave Temporal's databases out of the run"
    )
    parser.add_argument("--report", type=Path, help="also write the JSON report to this file")
    parser.add_argument(
        "--rto-seconds",
        type=float,
        help="also fail when the restore takes longer (a fixture-size measure, not the site's RTO)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the containers and volumes for inspection"
    )
    parser.add_argument(
        "--prune", action="store_true", help="remove every resource any drill run left, and exit"
    )
    options = parser.parse_args(argv)
    if shutil.which("docker") is None and not options.prune:
        print(
            json.dumps(
                {"drill": "restore", "ok": False, "error": "the docker CLI is not installed"}
            )
        )
        return 2
    if options.prune:
        problems = remove_run(None)
        for problem in problems:
            _say(problem)
        return 1 if problems else 0
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    report = run_drill(
        options.pg_image,
        options.temporal_image,
        not options.no_temporal,
        options.keep,
        options.rto_seconds,
    )
    text = json.dumps(report.as_dict(), indent=2)
    print(text)
    if options.report:
        options.report.write_text(text + "\n", encoding="utf-8")
    if _stop_signal is not None:
        _say(f"restore drill stopped by signal {_stop_signal}; its resources were removed")
        return 128 + _stop_signal
    if report.error:
        _say(f"restore drill could not complete: {report.error}")
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised through `make restore-drill`
    raise SystemExit(main())
