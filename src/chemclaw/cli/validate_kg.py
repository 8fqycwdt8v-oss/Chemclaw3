"""`make kg-validate`: the graph's own checks plus the citations only a store can answer.

Two checks that belong to different layers, run as one gate. `kg.validate` is pure — it reads a
notes directory and knows nothing about a database, which is what lets `kg` sit one edge above
`core` and nothing else. The citation-existence half needs the ELN transcription tier, and `ingest`
depends on `kg`, so the check cannot live there without inverting the layering for a one-method
need. It lives here instead, in the entrypoint layer that is allowed to see both — the same reason
`cli.validate_connectors` is a CLI rather than a `connectors` module.

**Why the second half exists at all.** Since D-2026-08-25 an ELN transcription is a row rather than
a file, so `kg.graph.dangling_links` deliberately does not report `[[reaction-<id>]]` as broken —
it cannot see the store. That trade is only acceptable because something else checks it, and this
is that something: CI runs with a Postgres service, so the check really runs there rather than
being a claim in a docstring.

When the database is unreachable the gate says so, loudly, and does **not** pass silently. A
validator that quietly skips is indistinguishable in a log from one that found nothing wrong, which
is the failure mode `map_to_hpc_identity` is remembered for.
"""

import asyncio
import sys
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.records import default_record_store
from chemclaw.kg.validate import external_citations, notes_in, unresolved_citations, validate


def main() -> int:
    """Validate the graph, then its external citations; print problems; return an exit code."""
    notes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.knowledge_path
    if not notes_dir.exists():
        print(f"notes directory does not exist: {notes_dir}")
        return 1
    try:
        problems = validate(notes_dir)
    except ChemclawError as exc:
        print(f"cannot determine this deployment's note vocabulary: {exc}")
        return 1

    citations = external_citations(notes_in(notes_dir))
    unchecked = 0
    if citations:
        try:
            problems.extend(asyncio.run(unresolved_citations(citations, default_record_store())))
        except Exception as exc:
            unchecked = len(citations)
            print(
                f"NOT CHECKED: {unchecked} reaction citation(s) were not verified against the "
                f"record store ({type(exc).__name__}: {exc}). This half of the gate needs "
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
            f"\n{unchecked} reaction citation(s) could not be checked, so this gate did not pass. "
            "Point CHEMCLAW_POSTGRES_DSN at a migrated database and run it again."
        )
        return 1
    checked = len(citations) - unchecked
    print(f"OK: {notes_dir} is a valid knowledge graph ({checked} reaction citation(s) verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
