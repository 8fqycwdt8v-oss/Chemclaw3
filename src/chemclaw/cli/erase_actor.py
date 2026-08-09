"""Offboard a person's data from the terminal: preview it, then apply it.

    python -m chemclaw.cli.erase_actor <oid>            # dry run: counts, deletes nothing
    python -m chemclaw.cli.erase_actor <oid> --apply    # commits

The thin `main()` shim over `chemclaw.agent.leaver`, which holds the two-tier rule and the reason
for it. Read that module before running this: it deletes the conversation and keeps the GxP record,
and the second half is not a limitation to work around.

Dry run by default because this is the one irreversible operation an operator performs on live data
whose correct target is a string somebody pasted from a directory.
"""

import argparse
import asyncio
import sys

from chemclaw.agent.leaver import ErasureReport, erase_actor, retention_reasons
from chemclaw.core.logging import configure_logging


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
    lines += ["", "RETAINED (the GxP record — not erasable by this command):"]
    for table, count in report.retained.items():
        lines.append(f"  {count:>7}  {table}")
        if count:
            lines.append(f"           {reasons[table]}")
    lines.append(f"  {report.retained_total:>7}  total")

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


def main(argv: list[str] | None = None) -> int:
    """Preview or apply one actor's erasure; exit non-zero if it could not run.

    `argv` is a parameter so a test can drive the real entry point rather than assert something
    about it — the shipped test for this path asserted `issubclass(psycopg.OperationalError,
    Exception)`, which is true of every exception and would have passed with this error handling
    deleted.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("actor", help="The Entra oid (or dev actor id) to erase.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the deletion. Without it, the counts are real and nothing is written.",
    )
    args = parser.parse_args(argv)
    configure_logging()
    try:
        report = asyncio.run(erase_actor(args.actor, apply=args.apply))
    # `ValueError` covers `ErasureError` — the seam translates a refused statement into one, so a
    # missing `DELETE ON session_owners` grant (the likeliest failure the first operator will hit)
    # prints instead of raising, and this entry point still needs no database driver of its own.
    except (ValueError, ConnectionError) as exc:
        print(f"erasure failed: {exc}", file=sys.stderr)
        return 1
    print(_render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
