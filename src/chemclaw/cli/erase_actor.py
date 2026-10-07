"""Offboard a person's data from the terminal: preview it, then apply it.

    python -m chemclaw.cli.erase_actor <oid>            # dry run: counts, deletes nothing
    python -m chemclaw.cli.erase_actor <oid> --apply    # commits
    python -m chemclaw.cli.erase_actor --finish <session-id> ... [--apply]

`--finish` deletes by id the orphaned sessions an interrupted actor run printed (exit `2`), refusing
any that still has an ownership row. A shim over `chemclaw.agent.leaver`: delete the conversation,
keep the record. Dry run by default.
"""

import argparse
import asyncio
import sys

from chemclaw.agent.leaver import (
    ErasureReport,
    ResidueReport,
    erase_actor,
    finish_erasure,
    finish_leaves,
    retention_reasons,
    unreachable_tables,
)
from chemclaw.core.logging import configure_logging


def _wrapped(why: str, *, width: int = 88, indent: str = " " * 11) -> list[str]:
    """One reason, wrapped to a terminal rather than run out to 150 columns on one line."""
    import textwrap

    return textwrap.wrap(why, width=width, initial_indent=indent, subsequent_indent=indent)


def _render(report: ErasureReport) -> str:
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


def main(argv: list[str] | None = None) -> int:
    """Preview or apply one actor's erasure; exit non-zero if it could not run or did not finish.

    `1` means it did not run (refusal, bad actor, declined statement); `2` means it ran, wrote and
    did not finish, which `--finish` remedies. Actor and `--finish` share one entry point because
    they are two halves of one operation. `argv` is a parameter so tests drive the real entry point.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "actor",
        nargs="?",
        help="The Entra oid (or dev actor id) to erase. Omit it when using --finish.",
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
        "--apply",
        action="store_true",
        help="Commit the deletion. Without it, the counts are real and nothing is written.",
    )
    args = parser.parse_args(argv)
    # "Exactly one of them", which argparse's mutually-exclusive group cannot express once `actor`
    # is optional.
    if bool(args.actor) == bool(args.finish):
        parser.error("give an actor id, or --finish with one or more session ids — not both")
    configure_logging()
    # `ValueError` covers `ErasureError`, into which the seam translates a refused statement (e.g. a
    # missing `DELETE ON session_owners` grant), so it prints rather than raises.
    if args.finish:
        try:
            finished = asyncio.run(finish_erasure(args.finish, apply=args.apply))
        except (ValueError, ConnectionError) as exc:
            print(f"erasure failed: {exc}", file=sys.stderr)
            return 1
        print(_render_finish(finished))
        # A run that wrote and did not finish must not read as success; a dry run wrote nothing.
        return 2 if args.apply and not finished.finished else 0
    try:
        report = asyncio.run(erase_actor(args.actor, apply=args.apply))
    except (ValueError, ConnectionError) as exc:
        print(f"erasure failed: {exc}", file=sys.stderr)
        return 1
    print(_render(report))
    # Non-zero on a residue: it ran, wrote, and did not finish.
    return 2 if report.residue else 0


if __name__ == "__main__":
    raise SystemExit(main())
