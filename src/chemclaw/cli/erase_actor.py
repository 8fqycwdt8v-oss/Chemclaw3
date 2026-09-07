"""Offboard a person's data from the terminal: preview it, then apply it.

    python -m chemclaw.cli.erase_actor <oid>            # dry run: counts, deletes nothing
    python -m chemclaw.cli.erase_actor <oid> --apply    # commits
    python -m chemclaw.cli.erase_actor --finish <session-id> ... [--apply]

The third form finishes an erasure that a live turn interrupted. The second form reports it, prints
the session ids and exits `2`; this one deletes those sessions **by id**, a route that never reads
`session_owners` and therefore reaches exactly the rows the actor form no longer can. It refuses any
session that still has an ownership row, so it cannot be used as an unscoped conversation delete.

The thin `main()` shim over `chemclaw.agent.leaver`, which holds the two-tier rule and the reason
for it. Read that module before running this: it deletes the conversation and keeps the record,
and the second half is not a limitation to work around.

Dry run by default because this is the one irreversible operation an operator performs on live data
whose correct target is a string somebody pasted from a directory.
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
    """One reason, wrapped to a terminal rather than run out to 150 columns on one line.

    The retained tier's reasons are short enough to sit on one line and the out-of-reach tier's are
    not, because they have to say what an operator would have to do instead. Wrapping here rather
    than shortening the prose: the reason is the substantive half of that tier's answer.
    """
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

    # The third tier, and the reason it is printed rather than left out: a table this command can
    # neither clear nor count is a question it did not answer, and an operator signing off on an
    # erasure has to see the unanswered ones. A count would be the honest thing to show and is
    # exactly what is unavailable, so the table is named instead — and the block is skipped only
    # when the register is empty, which is a real state rather than the "unconditionally" an
    # earlier version of this comment claimed two lines above the `if`.
    #
    # Indented to the same column as a table name in the two tiers above — `"  " + 7 + "  "` is
    # eleven characters — because a report whose three sections do not line up reads as three
    # reports.
    beyond = unreachable_tables()
    if beyond:
        lines += ["", "OUT OF REACH (named a person; this command can neither clear nor count):"]
        for table, why in beyond:
            lines += [f"{'':>11}{table}", *_wrapped(why)]

    # **The section that must not be a footnote.** A residue is a row that came back under a session
    # id whose ownership row is gone, and every session-scoped sweep in this system finds a session
    # through that row — so "run it again" is not the remedy, and a report that printed the erased
    # counts and nothing else would read as a completed erasure. Printed before the two closing
    # notes so it is the last thing an operator reads about what happened.
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

    # Printed on every run, not only when something was left: a table this route deliberately does
    # not clear is a question it did not answer, and the whole argument of this command is that an
    # unanswered question must be visible rather than absent from the report.
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

    Three exit codes rather than two: `1` is "it did not run" (a refusal, a bad actor, a statement
    the database declined), and `2` is "it ran, it wrote, and it did not finish" — a state that must
    not be scriptable as a success and is not the same event as a failure to start. `2` now has a
    remedy an operator can run, which is what `--finish` is; before it, the exit code named a
    condition with no next command.

    **`actor` and `--finish` are one entry point rather than two commands**, because they are two
    halves of one operation: the actor run is what discovers the orphaned session ids, prints them
    and exits `2`, and the finish run is what clears them. Splitting them across two modules would
    put the remedy somewhere an operator reading the failure has to be told about separately.

    `argv` is a parameter so a test can drive the real entry point rather than assert something
    about it — the shipped test for this path asserted `issubclass(psycopg.OperationalError,
    Exception)`, which is true of every exception and would have passed with this error handling
    deleted.
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
    # Checked here rather than with argparse's mutually-exclusive group, because the rule is
    # "exactly one of them" and that group only expresses "at most one" once `actor` is optional.
    if bool(args.actor) == bool(args.finish):
        parser.error("give an actor id, or --finish with one or more session ids — not both")
    configure_logging()
    # `ValueError` covers `ErasureError` in both arms — the seam translates a refused statement into
    # one, so a missing `DELETE ON session_owners` grant (the likeliest failure the first operator
    # will hit) prints instead of raising, and this entry point still needs no database driver of
    # its own.
    if args.finish:
        try:
            finished = asyncio.run(finish_erasure(args.finish, apply=args.apply))
        except (ValueError, ConnectionError) as exc:
            print(f"erasure failed: {exc}", file=sys.stderr)
            return 1
        print(_render_finish(finished))
        # Same rule as the actor form: a run that wrote and did not finish must not read as a
        # success. A dry run is neither — it wrote nothing, so there is nothing to have finished.
        return 2 if args.apply and not finished.finished else 0
    try:
        report = asyncio.run(erase_actor(args.actor, apply=args.apply))
    except (ValueError, ConnectionError) as exc:
        print(f"erasure failed: {exc}", file=sys.stderr)
        return 1
    print(_render(report))
    # Non-zero on a residue, for the reason the section above is printed at all: this ran, it wrote,
    # and it did not finish — an operator's script must not read that as a success.
    return 2 if report.residue else 0


if __name__ == "__main__":
    raise SystemExit(main())
