"""Carry compound notes and fingerprint rows across a `STANDARDIZATION_VERSION` bump.

    python -m chemclaw.cli.rekey_compounds            # preview: per-kind counts, writes nothing
    python -m chemclaw.cli.rekey_compounds --apply    # writes
    python -m chemclaw.cli.rekey_compounds --apply --dispose-superseded   # and then disposes

`make rekey-compounds [APPLY=1 [DISPOSE=1]]`, run after deploying a bump. A shim over
`chemclaw.memory.compound_rekey.rekey_standardization`; preview by default and idempotent.

The re-key adds the rebuilt generation and keeps the old one, so searches report `index_partial`
until `--dispose-superseded` deletes the superseded generation — per index, only when its rebuild
was complete. Disposal connects as the migrator (`core.migrate.migration_dsn`), never the runtime
role, and is opt-in because it is the one step that deletes and is wrong while an older image
still writes under the old definition.
"""

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta

from chemclaw.core.config import settings
from chemclaw.core.db import connection as db_connection
from chemclaw.core.migrate import migration_dsn
from chemclaw.kg.git_writer import default_writer
from chemclaw.memory.compound_rekey import StandardizationRekeyReport, rekey_standardization
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.rekey import FingerprintRekeyCounts
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.store import default_molecule_store, default_reaction_store


def render(report: StandardizationRekeyReport, seconds: float) -> str:
    """The operator's report: one line per kind, in the order the job does them."""
    verb = "APPLIED" if report.applied else "PREVIEW (nothing written; --apply to write)"
    lines = [f"{verb} — standardization {report.version}, {seconds:.2f} s", "", "compound notes:"]
    lines += [f"  {count:>7}  {kind}" for kind, count in report.notes.items()]
    for name, counts in (
        ("molecule fingerprints", report.molecules),
        ("reaction fingerprints", report.reactions),
    ):
        lines += ["", f"{name}:"]
        lines += [f"  {count:>7}  {kind}" for kind, count in counts.model_dump().items()]
    return "\n".join(lines)


async def rekey(*, apply: bool) -> StandardizationRekeyReport:
    """The re-key over this deployment's own notes and indexes.

    Shared by `main` and `cli/live_index.py`, so the lane runs exactly the operator's job.
    """
    return await rekey_standardization(
        apply=apply,
        notes_dir=settings.knowledge_path,
        writer=default_writer,
        molecule_store=default_molecule_store(),
        reaction_store=default_reaction_store(),
        # The last day an old id was current. `valid_to` is inclusive, so closing it today
        # would leave both notes current — both served, no redirect — until midnight.
        as_of=date.today() - timedelta(days=1),
    )


async def dispose_superseded(table: str, definition: str) -> int:
    """Delete `table`'s rows stored under any definition other than `definition`; return how many.

    Runs under the schema owner. `table` is interpolated, so it is checked to be a plain identifier.
    """
    if not table.isidentifier():
        raise ValueError(f"table must be a plain SQL identifier, got {table!r}")
    async with db_connection(migration_dsn(), operation="rekey-dispose-superseded") as conn:
        cursor = await conn.execute(
            f"DELETE FROM {table} WHERE definition <> %(definition)s", {"definition": definition}
        )
        return cursor.rowcount


async def settle_index(
    kind: str, table: str, definition: str, counts: FingerprintRekeyCounts
) -> str:
    """Dispose of `table`'s shelved generation when the re-key rebuilt all of it, and say which.

    Complete means `unreadable == 0`: every other shelved row was rebuilt or already current. An
    unreadable row has no current twin, so deleting the shelf would lose it silently; it stays and
    `index_partial` keeps saying so.
    """
    if counts.unreadable:
        return (
            f"{kind}: {counts.unreadable} shelved row(s) could not be rebuilt, so the superseded "
            "generation is kept and searches still say PARTIAL — re-sync those entries from source"
        )
    removed = await dispose_superseded(table, definition)
    return (
        f"{kind}: {counts.rekeyed} re-fingerprinted, {counts.current} already current, "
        f"{removed} superseded row(s) disposed of"
    )


async def settle_indexes(report: StandardizationRekeyReport) -> list[str]:
    """`settle_index` for both fingerprint indexes of an applied re-key, one line each.

    Shared by `main` and `cli/live_index.py`, so the lane disposes under the same guard.
    """
    if not report.applied:
        raise ValueError("a preview rebuilt nothing, so nothing it saw may be disposed of")
    return [
        await settle_index(
            "reaction fingerprints",
            "reaction_fingerprints",
            reaction_definition(),
            report.reactions,
        ),
        await settle_index(
            "molecule fingerprints",
            "molecule_fingerprints",
            molecule_definition(),
            report.molecules,
        ),
    ]


async def _run(*, apply: bool, dispose: bool) -> tuple[StandardizationRekeyReport, list[str]]:
    """The re-key, then — only when asked and only after it was applied — the disposal."""
    report = await rekey(apply=apply)
    return report, (await settle_indexes(report) if dispose else [])


def main(argv: list[str] | None = None) -> int:
    """Preview or apply the re-key and print the counts; `argv` is a parameter for a test."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the successors, the retirements and the rebuilt rows. Without it nothing is.",
    )
    parser.add_argument(
        "--dispose-superseded",
        action="store_true",
        help=(
            "After an applied re-key, delete each fingerprint index's superseded generation — "
            "only for an index whose rebuild was complete. Connects as the schema owner "
            "(CHEMCLAW_POSTGRES_MIGRATION_DSN when set). Requires --apply."
        ),
    )
    args = parser.parse_args(argv)
    if args.dispose_superseded and not args.apply:
        parser.error("--dispose-superseded requires --apply: a preview rebuilds nothing")
    started = time.perf_counter()
    report, disposed = asyncio.run(_run(apply=args.apply, dispose=args.dispose_superseded))
    print(render(report, time.perf_counter() - started))
    if disposed:
        print("\n".join(["", "superseded generations:", *(f"  {line}" for line in disposed)]))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
