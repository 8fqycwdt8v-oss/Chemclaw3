"""`make kg-validate`: the graph's own checks plus the citations only a store can answer.

Checks that belong to different layers, run as one gate. `kg.validate` is pure — it reads a
notes directory and knows nothing about a database, which is what lets `kg` sit one edge above
`core` and nothing else. The existence halves need the stores — the ELN transcription tier for
`[[reaction-*]]` citations, the calculation cache for `calc_refs` — and `ingest` and `science`
both depend on `kg`, so the checks cannot live there without inverting the layering for a
one-method need. They live here instead, in the entrypoint layer that is allowed to see both —
the same reason `cli.validate_connectors` is a CLI rather than a `connectors` module.

**Why the second half exists at all.** Since D-2026-08-25 an ELN transcription is a row rather than
a file, so `kg.graph.dangling_links` deliberately does not report `[[reaction-<id>]]` as broken —
it cannot see the store. That trade is only acceptable because something else checks it, and this
is that something: CI runs with a Postgres service, so the check really runs there rather than
being a claim in a docstring.

When the database is unreachable the gate says so, loudly, and does **not** pass silently. A
validator that quietly skips is indistinguishable in a log from one that found nothing wrong, which
is the failure mode `map_to_hpc_identity` is remembered for.
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


<<<<<<< HEAD
def main() -> int:
    """Validate the graph, then its store-backed citations; print problems; return an exit code."""
    notes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.knowledge_path
=======
def main(argv: Sequence[str] | None = None) -> int:
    """Validate the graph, then its external citations; print problems; return an exit code.

    The notes directory is a real positional and stays one — this gate is genuinely run against a
    dedicated note checkout (`CHEMCLAW_NOTE_REPO_DIR`) as well as the shipped tree. What it did not
    have was a *declaration*: reading `sys.argv[1]` raw meant `--help` was taken for a directory
    name and reported as missing, and a second argument was discarded in silence.
    """
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_kg",
        description="Validate a knowledge-graph notes directory: schema, duplicate ids, broken "
        "links, and reaction citations against the record store.",
    )
    parser.add_argument(
        "notes_dir",
        nargs="?",
        default=None,
        help=f"the notes directory to validate (default: {settings.knowledge_path}).",
    )
    options = parser.parse_args(argv)
    notes_dir = Path(options.notes_dir) if options.notes_dir else settings.knowledge_path
>>>>>>> origin/main
    if not notes_dir.exists():
        print(f"notes directory does not exist: {notes_dir}")
        return 1
    try:
        # One parse for all the halves: the citation checks read the same corpus `validate` just
        # walked, and the second `read_note` loop this used to run doubled the gate's cost.
        problems, notes = validate_with_notes(notes_dir)
    except ChemclawError as exc:
        print(f"cannot determine this deployment's note vocabulary: {exc}")
        return 1

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
        # The calculation half of the same question: `_calc_ref_shape` checks a ref's *form* and
        # concedes existence "is a question only a database can answer" — this is where it is
        # answered. Same store the cache writes (`calculation_results`), same failure posture.
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
        # **Non-zero, not zero.** This module's own docstring promised the gate "does not pass
        # silently" when the store is unreachable, and then returned success anyway — the printed
        # line was the whole of the control. It matters more here than it would elsewhere: since
        # D-2026-08-25 `dangling_links` ignores every `reaction-` target on purpose, so this half is
        # the *only* thing standing between a typo'd run id and a merge. A gate that cannot run its
        # one remaining check has not passed; it has not looked.
        print(
            f"\n{unchecked} store-backed citation(s) could not be checked, so this gate did not "
            "pass. Point CHEMCLAW_POSTGRES_DSN at a migrated database and run it again."
        )
        return 1
    print(
        f"OK: {notes_dir} is a valid knowledge graph "
        f"({len(citations)} reaction citation(s) and {len(calc_refs)} calc_ref(s) verified)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
