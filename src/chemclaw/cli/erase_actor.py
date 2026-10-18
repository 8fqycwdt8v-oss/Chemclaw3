"""Offboard a person's data from the terminal: preview it, then apply it.

    python -m chemclaw.cli.erase_actor <oid>            # dry run: counts, deletes nothing
    python -m chemclaw.cli.erase_actor <oid> --apply    # commits, and records it in the ledger
    python -m chemclaw.cli.erase_actor --finish <session-id> ... [--apply]
    python -m chemclaw.cli.erase_actor --replay-since <recovery point> [--apply]

`--finish` deletes by id the orphaned sessions an interrupted actor run printed (exit `2`), refusing
any that still has an ownership row. A shim over `chemclaw.agent.leaver`: delete the conversation,
keep the record. Dry run by default.

Every applied erasure appends its record to the erasure ledger (`core/erasure_ledger.py`) the moment
it commits, and `--apply` with no ledger and no stated posture is refused before anything is erased.
After a database restore, `--replay-since` re-applies every erasure recorded after the recovery
point, each in the tenant its record names; re-applying one removes nothing it did not.

With tenancy enabled a person's rows are a tenant's, so the run names it: `--tenant <id>`, or
`--all-tenants` to erase a person who works in several (an actor run only). With neither it refuses.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from chemclaw.agent.leaver import (
    ErasureReport,
    ResidueReport,
    erase_actor,
    finish_erasure,
    finish_leaves,
    retention_reasons,
    unreachable_tables,
)
from chemclaw.core import erasure_ledger
from chemclaw.core.config import settings
from chemclaw.core.erasure_ledger import ErasureRecord, LedgerError
from chemclaw.core.logging import configure_logging
from chemclaw.core.tenancy import TenantError, bound_tenant
from chemclaw.core.tenant_cli import add_tenant_flags, for_chosen_tenants, refusal
from chemclaw.core.tenant_registry import choose_tenants

#: The exit status of an erasure that committed and whose record could not be written.
EXIT_UNRECORDED = 3


class _Unrecorded(Exception):
    """An erasure committed and its ledger line could not be written.

    Deliberately not a `ValueError`, so no route's "erasure failed" handler can report it as an
    erasure that did not happen.
    """

    def __init__(self, record: ErasureRecord, cause: OSError) -> None:
        super().__init__(str(cause))
        self.record = record


def _wrapped(why: str, *, width: int = 88, indent: str = " " * 11) -> list[str]:
    """One reason, wrapped to a terminal rather than run out to 150 columns on one line."""
    import textwrap

    return textwrap.wrap(why, width=width, initial_indent=indent, subsequent_indent=indent)


def _render(report: ErasureReport, tenant: str = "") -> str:
    """The operator-facing report: both tiers, counts per table, and why the second one stays."""
    lines = [
        f"actor: {report.actor}",
        "",
        ("ERASED" if report.applied else "WOULD ERASE") + " (the conversation):",
    ]
    for table, count in report.erased.items():
        lines.append(f"  {count:>7}  {table}")
    lines.append(f"  {report.erased_total:>7}  total")

    reasons = dict(retention_reasons())
    lines += ["", "RETAINED (the record of who did what — not erasable by this command):"]
    for table, count in report.retained.items():
        lines.append(f"  {count:>7}  {table}")
        if count:
            lines.append(f"           {reasons[table]}")
    lines.append(f"  {report.retained_total:>7}  total")

    # The third tier: tables this command can neither clear nor count are named, since an operator
    # signing off must see what was not answered. Skipped only when the register is empty. Indented
    # to eleven columns to align with the table names above.
    beyond = unreachable_tables()
    if beyond:
        lines += ["", "OUT OF REACH (named a person; this command can neither clear nor count):"]
        for table, why in beyond:
            lines += [f"{'':>11}{table}", *_wrapped(why)]

    # Residue: rows under a session whose ownership row is gone, which no session-scoped sweep can
    # find again, so "run it again" is not the remedy. Printed before the closing notes so it is the
    # last thing read.
    if report.residue:
        lines += [
            "",
            "INCOMPLETE — these rows came back while the erasure ran; no actor-scoped erasure "
            "can reach them:",
        ]
        for table, count in sorted(report.residue.items()):
            lines.append(f"  {count:>7}  {table}")
        lines += _wrapped(
            "A turn was running on one of this person's sessions. Their ownership rows are gone, "
            "so no actor-scoped erasure can find these sessions again — but they can still be "
            "deleted by id. Stop the turn, then run:",
            indent="",
        )
        lines += [
            "",
            "  python -m chemclaw.cli.erase_actor --finish "
            + " ".join(report.residue_sessions)
            + (f" --tenant {tenant}" if tenant else "")
            + " --apply",
        ]

    if not report.applied:
        lines += ["", "Nothing was written. Re-run with --apply to commit."]
    elif report.retained_total:
        lines += [
            "",
            f"{report.retained_total} row(s) still attribute work to this actor. That is "
            "deliberate; if your data-protection obligation reaches them, it is a decision to take "
            "with the record's owner, not a flag on this command.",
        ]
    return "\n".join(lines)


def _render_finish(report: ResidueReport) -> str:
    """The operator-facing report for the finish route: what went, what stayed, what was refused."""
    lines = [
        "sessions: " + " ".join(report.sessions),
        "",
        ("REMOVED" if report.applied else "WOULD REMOVE") + " (an orphaned conversation's rows):",
    ]
    for table, count in report.removed.items():
        lines.append(f"  {count:>7}  {table}")
    lines.append(f"  {report.removed_total:>7}  total")

    # Printed on every run: what this route deliberately does not clear must be visible, not absent.
    lines += ["", "LEFT BEHIND (not this route's to delete):"]
    for table, why in finish_leaves():
        lines += [f"{'':>11}{table}", *_wrapped(why)]

    if report.refused:
        lines += ["", "REFUSED (still owned — this route only finishes an orphaned session):"]
        for session_id, why in sorted(report.refused.items()):
            lines += [f"{'':>11}{session_id}", *_wrapped(why)]

    if report.remaining:
        lines += ["", "STILL THERE — another turn wrote while this ran; stop it and run it again:"]
        for table, count in sorted(report.remaining.items()):
            lines.append(f"  {count:>7}  {table}")

    if not report.applied:
        lines += ["", "Nothing was written. Re-run with --apply to commit."]
    elif report.finished:
        lines += ["", "The erasure is finished: nothing this route can reach names these sessions."]
    return "\n".join(lines)


def _recorder(
    ledger: Path | None,
    tenant: str,
    *,
    actor: str | None = None,
    sessions: Sequence[str] = (),
) -> Callable[[datetime], None]:
    """What an applied erasure calls the moment it commits: append its record, or print it.

    Printed (to copy into the operator's own log) when the posture is that the record is kept by
    hand; otherwise appended, and a failure is raised as `_Unrecorded` with the line in it.
    """

    def record(at: datetime) -> None:
        entry = ErasureRecord(at=at, tenant=tenant, actor=actor, sessions=tuple(sessions))
        if ledger is None:
            print(f"RECORD THIS ERASURE in your log (kept by hand): {entry.line()}", flush=True)
            return
        try:
            erasure_ledger.append(ledger, entry)
        except OSError as exc:
            raise _Unrecorded(entry, exc) from exc

    return record


def _erase_route(args: argparse.Namespace, ledger: Path | None) -> int:
    """One actor, in each chosen tenant."""
    try:
        reports = asyncio.run(
            for_chosen_tenants(
                args,
                lambda tenant: erase_actor(
                    args.actor,
                    apply=args.apply,
                    on_commit=_recorder(ledger, tenant, actor=args.actor),
                ),
            )
        )
    except TenantError as exc:
        return refusal(exc, status=1)
    except (ValueError, ConnectionError) as exc:
        print(f"erasure failed: {exc}", file=sys.stderr)
        return 1
    for tenant, report in reports.items():
        if len(reports) > 1:
            print(f"== tenant {tenant} ==")
        print(_render(report, tenant if settings.tenancy_enabled else ""))
    if args.apply and ledger is not None:
        print(f"\nRecorded in the erasure ledger {ledger}.")
    # Non-zero on a residue: it ran, wrote, and did not finish.
    return 2 if any(report.residue for report in reports.values()) else 0


def _finish_route(args: argparse.Namespace, ledger: Path | None) -> int:
    """Orphaned sessions by id, in the one chosen tenant."""
    try:
        done = asyncio.run(
            for_chosen_tenants(
                args,
                lambda tenant: finish_erasure(
                    args.finish,
                    apply=args.apply,
                    on_commit=_recorder(ledger, tenant, sessions=args.finish),
                ),
            )
        )
    except TenantError as exc:
        return refusal(exc, status=1)
    except (ValueError, ConnectionError) as exc:
        print(f"erasure failed: {exc}", file=sys.stderr)
        return 1
    (finished,) = done.values()
    print(_render_finish(finished))
    # A run that wrote and did not finish must not read as success; a dry run wrote nothing.
    return 2 if args.apply and not finished.finished else 0


async def _reapply(records: list[ErasureRecord], *, apply: bool, ledger: Path | None) -> bool:
    """Re-apply each record in its own tenant, in ledger order; whether every one finished.

    Order matters for one pair only: an actor erasure before a later finish of its residue, which
    would otherwise be refused as still owned.
    """
    finished = True
    for record in records:
        (tenant,) = await choose_tenants(record.tenant)
        print(f"== the erasure recorded at {record.at.isoformat()} ({record.route}, {tenant}) ==")
        on_commit = _recorder(ledger, tenant, actor=record.actor, sessions=record.sessions)
        with bound_tenant(tenant):
            if record.actor is None:
                residue = await finish_erasure(
                    list(record.sessions), apply=apply, on_commit=on_commit
                )
                print(_render_finish(residue))
                finished = finished and (residue.finished or not apply)
            else:
                report = await erase_actor(record.actor, apply=apply, on_commit=on_commit)
                print(_render(report, tenant if settings.tenancy_enabled else ""))
                finished = finished and not report.residue
    return finished


def _replay_route(args: argparse.Namespace) -> int:
    """After a restore: re-apply every erasure the ledger records after the recovery point.

    Each re-applied erasure is recorded again, so a later restore to a point before this replay
    finds it too. The records are read before the ledger is opened for appending, because opening
    creates it and a missing ledger must refuse rather than replay nothing.

    Raises:
        LedgerError: no ledger is configured, it cannot be read, or the instant does not parse.
    """
    if not settings.erasure_ledger_path:
        raise LedgerError(
            "nothing to replay from: CHEMCLAW_ERASURE_LEDGER_PATH is unset. If the record is kept "
            "by hand, re-run each erasure recorded after the recovery point with "
            "`python -m chemclaw.cli.erase_actor <id> --tenant <tenant> --apply`"
        )
    since = erasure_ledger.parse_instant(args.replay_since)
    records = erasure_ledger.records_since(Path(settings.erasure_ledger_path), since)
    ledger = erasure_ledger.ledger_for_apply() if args.apply else None
    if not records:
        print(f"The ledger records no erasure after {since.isoformat()}: nothing to re-apply.")
        return 0
    try:
        finished = asyncio.run(_reapply(records, apply=args.apply, ledger=ledger))
    except TenantError as exc:
        return refusal(exc, status=1)
    except (ValueError, ConnectionError) as exc:
        print(f"erasure failed: {exc}", file=sys.stderr)
        return 1
    verb = "RE-APPLIED" if args.apply else "WOULD RE-APPLY"
    print(f"\n{verb} {len(records)} erasure(s) recorded after {since.isoformat()}.")
    return 0 if finished else 2


def main(argv: list[str] | None = None) -> int:
    """Preview or apply one erasure, or replay the ledger; exit non-zero unless it ran and finished.

    `1` means it did not run (refusal, bad actor, declined statement); `2` means it ran, wrote and
    did not finish, which `--finish` remedies; `3` means an erasure committed and its record could
    not be written, which re-running the same command remedies (it is idempotent and records
    itself). The routes share one entry point because they are parts of one operation. `argv` is a
    parameter so tests drive the real entry point.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "actor",
        nargs="?",
        help="The Entra oid (or dev actor id) to erase. Omit it with --finish or --replay-since.",
    )
    parser.add_argument(
        "--finish",
        nargs="+",
        metavar="SESSION_ID",
        help=(
            "Finish an interrupted erasure by deleting these orphaned sessions by id. The ids are "
            "the ones an actor run printed when it exited 2. A session that still has an ownership "
            "row is refused."
        ),
    )
    parser.add_argument(
        "--replay-since",
        metavar="TIME",
        help=(
            "After a restore: re-apply every erasure the ledger records after TIME, the recovery "
            "point the session database was restored to (e.g. '2026-10-14 09:41:00+00'). Each "
            "runs in the tenant its record names."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the deletion. Without it, the counts are real and nothing is written.",
    )
    add_tenant_flags(parser, all_tenants=True)
    args = parser.parse_args(argv)
    # "Exactly one of them", which argparse's mutually-exclusive group cannot express once `actor`
    # is optional.
    if sum(map(bool, (args.actor, args.finish, args.replay_since))) != 1:
        parser.error("give an actor id, --finish with session ids, or --replay-since — exactly one")
    if args.finish and args.all_tenants:
        parser.error("--finish names sessions, which belong to one tenant: use --tenant")
    if args.replay_since and (args.tenant or args.all_tenants):
        parser.error("--replay-since acts in the tenant each record names: drop the tenant flags")
    configure_logging()
    # `ValueError` covers `ErasureError`, into which the seam translates a refused statement (e.g. a
    # missing `DELETE ON session_owners` grant), so it prints rather than raises.
    try:
        if args.replay_since:
            return _replay_route(args)
        # Before anything is erased: an applied erasure a restore cannot find is the defect.
        ledger = erasure_ledger.ledger_for_apply() if args.apply else None
        return _finish_route(args, ledger) if args.finish else _erase_route(args, ledger)
    except LedgerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except _Unrecorded as exc:
        print(
            f"the erasure COMMITTED and its record could not be written ({exc}). Fix the ledger "
            "and run the same command again: it is idempotent and records itself. Or append this "
            f"line to the ledger by hand:\n{exc.record.line()}",
            file=sys.stderr,
        )
        return EXIT_UNRECORDED


if __name__ == "__main__":
    raise SystemExit(main())
