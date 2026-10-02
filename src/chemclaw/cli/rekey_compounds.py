"""Carry compound notes and fingerprint rows across a `STANDARDIZATION_VERSION` bump.

    python -m chemclaw.cli.rekey_compounds            # preview: per-kind counts, writes nothing
    python -m chemclaw.cli.rekey_compounds --apply    # writes

`make rekey-compounds [APPLY=1]`. Run it after deploying a bump. The thin shim over
`chemclaw.memory.compound_rekey.rekey_standardization`, which says what it writes and why;
`D-2026-09-27-a-compound-id-a-bump-moves-is-superseded-not-orphaned` is the decision.

A preview by default for the reason `erase_actor` and `rekey_campaigns` give: a run is reviewable
before it runs only if the bare command writes nothing. It is idempotent, so running it twice is
safe, and the second run's counts say so.
"""

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta

from chemclaw.core.config import settings
from chemclaw.kg.git_writer import default_writer
from chemclaw.memory.compound_rekey import StandardizationRekeyReport, rekey_standardization
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
    """The re-key over this deployment's own notes and indexes — the one binding of its arguments.

    Shared by `main` and by the live lane's index step (`cli/live_index.py`), so the lane runs
    exactly the job an operator runs rather than a second spelling of it.
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


def main(argv: list[str] | None = None) -> int:
    """Preview or apply the re-key and print the counts; `argv` is a parameter for a test."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the successors, the retirements and the rebuilt rows. Without it nothing is.",
    )
    args = parser.parse_args(argv)
    started = time.perf_counter()
    report = asyncio.run(rekey(apply=args.apply))
    print(render(report, time.perf_counter() - started))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
