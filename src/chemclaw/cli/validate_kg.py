"""`make kg-validate`: the graph's own checks plus the citations only a store can answer.

`kg.validate` is pure and knows no database. The existence checks need stores — the ELN
transcription tier for `[[reaction-*]]` citations, the calculation cache for `calc_refs` — and
`ingest`/`science` depend on `kg`, so they live in this entrypoint layer. `dangling_links` ignores
`[[reaction-<id>]]` targets on purpose, which makes this gate their only check; CI runs it with a
Postgres service.

An unreachable database or an empty corpus fails the gate loudly rather than passing silently.

`--compare` is a different question: whether the notes directory and the Postgres store serve the
same canonical notes and edges (`kg.compare`). Exit 0 when they do, 1 when any note differs, 2 when
the comparison could not run (2 also when tenancy is on and no `--tenant` names whose store is
compared: one tenant at a time). A difference must persist across `--settle-seconds` (default
`kg_compare_settle_seconds`) to be reported. `--repair` (under `kg_store=dual` only) then copies the
directory's version of each differing note into Postgres, and refuses (exit 2) unless the directory
is a checkout at the remote's tip: from one behind it, it would put old notes over newer ones.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.db import pooling
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tenancy import TenantError
from chemclaw.core.tenant_cli import add_tenant_flags, for_chosen_tenants, refusal
from chemclaw.ingest.eln.records import default_record_store
from chemclaw.kg.compare import Comparison, Repair, compare_stores, repair_from_tree
from chemclaw.kg.git_writer import GitNoteWriter
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.store import GitNoteStore
from chemclaw.kg.validate import (
    calc_citations,
    external_citations,
    unresolved_calc_refs,
    unresolved_citations,
    validate_with_notes,
)
from chemclaw.science.calc.postgres_store import PostgresStore

_Result = tuple[Comparison, Repair | None, Comparison | None]


async def _compare_for_bound_tenant(
    notes_dir: Path, settle_seconds: float, repair: bool
) -> _Result:
    """Compare the directory with the bound tenant's store; if asked, repair and compare again."""
    tree, store = GitNoteStore(notes_dir), PostgresNoteStore()
    found = await compare_stores(tree, store, settle_seconds=settle_seconds)
    if not repair or found.clean:
        return found, None, None
    repaired = await repair_from_tree(tree, store, found.differences)
    return found, repaired, await compare_stores(tree, store)


async def _compare(
    notes_dir: Path, options: argparse.Namespace, settle_seconds: float, repair: bool
) -> _Result:
    """The comparison for the one tenant the flags choose (tenancy off: the only one)."""
    async with pooling():
        chosen = await for_chosen_tenants(
            options,
            lambda _tenant: _compare_for_bound_tenant(notes_dir, settle_seconds, repair),
        )
    (result,) = chosen.values()
    return result


def _print_comparison(found: Comparison) -> None:
    print(f"git: {found.git_notes} note(s); postgres: {found.postgres_notes} note(s)")
    for difference in found.differences:
        detail = f" ({', '.join(difference.fields)})" if difference.fields else ""
        print(f"DIFFERENT {difference.note_id}: {', '.join(difference.kinds)}{detail}")


def _compare_main(options: argparse.Namespace, notes_dir: Path) -> int:
    """`--compare` (and `--repair`): print the report as text or JSON; return the exit code."""
    settle = (
        options.settle_seconds
        if options.settle_seconds is not None
        else settings.kg_compare_settle_seconds
    )
    if options.repair and settings.kg_store != "dual":
        print(
            f"--repair is for kg_store=dual, and this is kg_store={settings.kg_store}: with only "
            "the tree written, a difference is a store that needs `make kg-import`; with only "
            "Postgres written, the tree is the stale side.",
            file=sys.stderr,
        )
        return 2
    try:
        lagging = asyncio.run(GitNoteWriter(repo_dir=str(notes_dir)).lagging_reason())
        if lagging is not None and options.repair:
            print(f"--repair refused: {lagging}", file=sys.stderr)
            return 2
        if lagging is not None:
            print(f"WARNING: {lagging}; differences below may be that lag.", file=sys.stderr)
        found, repaired, after = asyncio.run(_compare(notes_dir, options, settle, options.repair))
    except TenantError as exc:
        return refusal(exc)
    except Exception as exc:
        print(f"the comparison could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    final = after if after is not None else found
    if options.json:
        report = {"comparison": found.as_json()}
        if repaired is not None and after is not None:
            report |= {"repair": repaired.as_json(), "after_repair": after.as_json()}
        print(json.dumps(report, indent=2))
    else:
        _print_comparison(found)
        if repaired is not None and after is not None:
            print(f"repaired {len(repaired.repaired)} note(s)")
            for note_id, reason in repaired.unrepaired:
                print(f"NOT REPAIRED {note_id}: {reason}")
            print(f"after repair: {len(after.differences)} difference(s)")
        print("OK: the stores serve the same notes" if final.clean else "DIFFERENT: see above")
    return 0 if final.clean else 1


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
    parser.add_argument(
        "--compare",
        action="store_true",
        help="compare the directory with the Postgres store instead of validating it.",
    )
    parser.add_argument(
        "--repair", action="store_true", help="with --compare: put right what differs (dual only)."
    )
    parser.add_argument("--json", action="store_true", help="with --compare: print JSON.")
    add_tenant_flags(parser, all_tenants=False)
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=None,
        help="with --compare: look again after this pause and report only what persists "
        "(default: kg_compare_settle_seconds; 0 looks once).",
    )
    options = parser.parse_args(argv)
    if (options.repair or options.json) and not options.compare:
        parser.error("--repair and --json belong to --compare")
    notes_dir = Path(options.notes_dir) if options.notes_dir else settings.knowledge_path
    # `is_dir()`, not `exists()`: a file would walk zero notes and pass.
    if not notes_dir.is_dir():
        print(f"notes directory does not exist, or is not a directory: {notes_dir}")
        return 1
    if options.compare:
        return _compare_main(options, notes_dir)
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
