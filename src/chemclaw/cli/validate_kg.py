"""`make kg-validate`: the graph's own checks plus the citations only a store can answer.

`kg.validate` is pure and knows no database. The existence checks need stores — the ELN
transcription tier for `[[reaction-*]]` citations, the calculation cache for `calc_refs` — and
`ingest`/`science` depend on `kg`, so they live in this entrypoint layer. `dangling_links` ignores
`[[reaction-<id>]]` targets on purpose, which makes this gate their only check; CI runs it with a
Postgres service.

An unreachable database or an empty corpus fails the gate loudly rather than passing silently.
"""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.records import default_record_store
from chemclaw.kg.validate import (
    calc_citations,
    external_citations,
    unresolved_calc_refs,
    unresolved_citations,
    validate_with_notes,
)
from chemclaw.science.calc.postgres_store import PostgresStore


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the graph, then its store-backed citations; print problems; return an exit code.

    The notes directory is a positional, run against the shipped tree or a dedicated note checkout
    (`CHEMCLAW_NOTE_REPO_DIR`).
    """
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_kg",
        description="Validate a knowledge-graph notes directory: schema, duplicate ids, broken "
        "links, and reaction and calculation citations against their stores.",
    )
    parser.add_argument(
        "notes_dir",
        nargs="?",
        default=None,
        help=f"the notes directory to validate (default: {settings.knowledge_path}).",
    )
    options = parser.parse_args(argv)
    notes_dir = Path(options.notes_dir) if options.notes_dir else settings.knowledge_path
    # `is_dir()`, not `exists()`: a file would walk zero notes and pass.
    if not notes_dir.is_dir():
        print(f"notes directory does not exist, or is not a directory: {notes_dir}")
        return 1
    try:
        # One parse for all the halves: the citation checks read the same corpus `validate` just
        # walked.
        problems, notes = validate_with_notes(notes_dir)
    except ChemclawError as exc:
        print(f"cannot determine this deployment's note vocabulary: {exc}")
        return 1

    if not notes:
        # Appended rather than returned early, so an unparseable sole file still reports its parse
        # failure. A corpus of zero notes is a problem, not a pass: a mis-set
        # `CHEMCLAW_NOTE_REPO_DIR` would otherwise turn off the only check on `[[reaction-*]]`
        # citations.
        problems.append(
            f"no notes found under {notes_dir} — this gate would have checked nothing. "
            "Check CHEMCLAW_NOTE_REPO_DIR / CHEMCLAW_KNOWLEDGE_DIR before reading this as a pass."
        )

    citations = external_citations(notes)
    calc_refs = calc_citations(notes)
    unchecked = 0
    if citations:
        try:
            problems.extend(asyncio.run(unresolved_citations(citations, default_record_store())))
        except Exception as exc:
            unchecked += len(citations)
            print(
                f"NOT CHECKED: {len(citations)} reaction citation(s) were not verified against "
                f"the record store ({type(exc).__name__}: {exc}). This half of the gate needs "
                "CHEMCLAW_POSTGRES_DSN to reach a migrated database."
            )
    if calc_refs:
        # The calculation half: `_calc_ref_shape` checks a ref's form; existence is checked here
        # against `calculation_results`, with the same failure posture.
        try:
            problems.extend(asyncio.run(unresolved_calc_refs(calc_refs, PostgresStore())))
        except Exception as exc:
            unchecked += len(calc_refs)
            print(
                f"NOT CHECKED: {len(calc_refs)} calc_ref(s) were not verified against the "
                f"calculation store ({type(exc).__name__}: {exc}). This half of the gate needs "
                "CHEMCLAW_POSTGRES_DSN to reach a migrated database."
            )

    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s) found in {notes_dir}")
        return 1
    if unchecked:
        # Non-zero: a gate that cannot run its store check has not passed, and this is the only
        # check on `[[reaction-*]]` citations.
        print(
            f"\n{unchecked} store-backed citation(s) could not be checked, so this gate did not "
            "pass. Point CHEMCLAW_POSTGRES_DSN at a migrated database and run it again."
        )
        return 1
    print(
        f"OK: {notes_dir} is a valid knowledge graph "
        f"({len(citations)} reaction citation(s) and {len(calc_refs)} calc_ref(s) verified)"
    )
    if not citations and not calc_refs:
        # Said out loud so a reader knows the database half had nothing to check. Not an error: the
        # shipped tree has no external citations (a seed `calc_ref` would fail on every fresh
        # database).
        print(
            "NOTE: this corpus cites no reaction record and no calculation, so the two "
            "store-backed halves of this gate had nothing to check. "
            "tests/test_kg_validate_store_arms.py is what drives them."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
