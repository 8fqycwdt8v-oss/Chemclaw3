"""ADR ids must actually identify a decision, and the ledger must match the files (D-088, D-147).

Two branches building in parallel each appended ADRs to the end of one `DECISIONS.md` and each
allocated the *next* free number as seen from its own base — so both wrote D-074, D-075, D-076,
D-081 and D-082, and the collision survived two merges because nothing looked. A duplicate id in an
append-only record is not cosmetic: `docs/planning/BACKLOG.md`, `docs/planning/DEFERRED.md`, the
design docs and several modules cite ADRs by number, and a citation that resolves to two different
decisions is worse than a dangling one — it reads as authoritative while pointing at the wrong
rationale.

D-147 split the log into one file per ADR, which turns that particular collision into an add/add
conflict on a filename rather than something to be noticed in prose. These checks are what is left
that a machine can still catch: an id that names two files, a file whose name and heading disagree,
and a ledger that has drifted from the files beside it.

Deliberately about *identity*, not formatting: the prose style of an ADR is a review matter.

Two later checks live here for the same reason and are about the index rather than the ids: the
"By topic" table, which is how a reader finds the *current* decision on a subject and which went
180 ADRs stale under four green assertions about the record table beside it; and the `test_*`
names an ADR cites in its "What keeps it true" section, which a rename retires in silence. Both
are bounds with a declared exception list, not measurements — see each constant.
"""

import re
from collections import Counter
from pathlib import Path

_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
_INDEX = _DECISIONS / "README.md"
# Two id shapes, and the second is the one new ADRs use.
#
# `D-NNN` is the original sequence. It is **frozen**: every numbered ADR keeps its name, and the
# citations to them across the tree keep resolving. A merged ADR can never collide — only an
# unallocated number was ever contended — so renaming them would buy nothing and break every
# citation. (Deliberately no counts here: they were wrong within a day of being written.)
#
# `D-YYYY-MM-DD-<slug>` is what an author writes now. The whole stem is the id — not the date —
# because two ADRs on one day is routine here, and an id that identifies two decisions is the exact
# thing this file exists to prevent. Collision then requires the same date *and* the same slug, and
# even that surfaces as an add/add conflict on a filename rather than as a silent duplicate.
#
# The change is not a preference. `D-147` split one `DECISIONS.md` into one file per ADR to make a
# collision loud, and it worked — but "highest on origin/main, plus one" is a read that is stale the
# instant another session pushes, and this repository runs many sessions at once. In a single day
# one branch renumbered three ADRs twice and another renumbered three times; five collisions, all
# on unallocated numbers. A date and a slug are knowable without consulting anything.
_NUMBERED = r"D-\d{3}"
_DATED = r"D-\d{4}-\d{2}-\d{2}-[a-z0-9-]+"
_FILENAME = re.compile(rf"^(?:{_NUMBERED}-[a-z0-9-]+|{_DATED})$")
_HEADING = re.compile(rf"^# ({_NUMBERED}|{_DATED}) — ", re.MULTILINE)
# The id cell tolerates a bare id (a legacy reservation) and a `[id](file.md)` link (written up).
_INDEX_ROW = re.compile(
    rf"^\| \[?({_NUMBERED}|{_DATED})\]?(?:\([^)]*\))? \| ([^|]*)\|", re.MULTILINE
)

# A ledger row for a number claimed but not yet written up. **Legacy**, kept because sessions had
# reservations in flight when the dated form landed and a convention change must not strand them.
# A dated id needs no reservation: it cannot be taken by anyone else, so there is nothing to claim.
#
# It mattered while it lasted: `CLAUDE.md` told an author to reserve in their *first* commit, which
# necessarily means the row exists before the ADR does, and without a marker for that state this
# test rejected the very convention the repo documented — `1f1f233` reserved D-124…D-129 as
# instructed and `8f6a319` had to delete five of them to get CI green.
_RESERVED = "RESERVED"


def _adr_id(path: Path) -> str:
    """The id a file carries: `D-NNN` for the numbered form, the whole stem for the dated one."""
    return path.stem[:5] if re.fullmatch(rf"{_NUMBERED}-.*", path.stem) else path.stem


def _sort_key(path: Path) -> tuple[int, int, str]:
    """Numbered ADRs first in numeric order, then dated ones chronologically.

    Two orderings in one sequence, because `int(stem[2:5])` cannot read a date and lexicographic
    order cannot read `D-9` against `D-10`. The ledger is asserted to match this exactly, so the
    key is the definition of "the record's order" rather than a display preference.
    """
    stem = path.stem
    if re.fullmatch(rf"{_NUMBERED}-.*", stem):
        return (0, int(stem[2:5]), "")
    return (1, 0, stem)


def _adr_files() -> list[Path]:
    """Every ADR file in record order — the record itself."""
    return sorted(_DECISIONS.glob("D-*.md"), key=_sort_key)


def _index_rows() -> list[tuple[str, str]]:
    """Every `(id, title)` in the allocation ledger, in file order."""
    return [(adr, title.strip()) for adr, title in _INDEX_ROW.findall(_INDEX.read_text("utf-8"))]


def _adr_ids() -> list[str]:
    """Every ADR id that has a file, in record order."""
    return [_adr_id(path) for path in _adr_files()]


def _index_ids() -> list[str]:
    """Every id in the ledger, reserved or written, in file order."""
    return [adr for adr, _ in _index_rows()]


def _written_ids() -> list[str]:
    """Ledger ids whose ADR is claimed to exist — every row that is not a reservation."""
    return [adr for adr, title in _index_rows() if not title.startswith(_RESERVED)]


def _reserved_ids() -> list[str]:
    """Ledger ids claimed by a branch whose ADR is not written yet."""
    return [adr for adr, title in _index_rows() if title.startswith(_RESERVED)]


def test_every_adr_id_is_unique() -> None:
    """No ADR number names two decisions — the invariant a parallel merge silently breaks.

    Since D-147 the filesystem enforces the common case (two files cannot share a name), but two
    *differently slugged* files can still carry the same number, which is exactly what two branches
    both writing D-074 would produce.
    """
    duplicates = sorted(adr for adr, count in Counter(_adr_ids()).items() if count > 1)
    assert not duplicates, f"docs/decisions/ reuses ADR ids: {duplicates}"


def test_every_filename_matches_its_heading() -> None:
    """The id in the filename is the id in the document — or a citation resolves to the wrong ADR.

    The filename is what the ledger links to and what a `git grep` for an ADR finds; the heading is
    what a reader sees. A file renamed without its heading is a mismatch nothing else here catches.
    """
    for path in _adr_files():
        assert _FILENAME.match(path.stem), (
            f"{path.name}: expected `D-NNN-lowercase-slug.md`; the ledger's links and every "
            "`git grep` for an ADR rely on that shape"
        )
        headings = _HEADING.findall(path.read_text("utf-8"))
        assert headings, f"{path.name} has no `# D-NNN — Title` heading"
        assert len(headings) == 1, f"{path.name} carries more than one ADR heading: {headings}"
        assert headings[0] == _adr_id(path), (
            f"{path.name} is titled {headings[0]}; filename and heading must name one decision"
        )


def test_the_index_lists_exactly_the_decisions_on_disk() -> None:
    """`docs/decisions/README.md` and the files beside it name the same ADRs, in the same order.

    The index exists so that "which numbers are taken?" is one listing against `origin/main` instead
    of a scan of the whole record — which is what makes it usable for allocating a number *before*
    writing the ADR. That only holds while the two agree. A ledger that has silently drifted is
    worse than no ledger: it is consulted, believed, and hands out a number somebody already used —
    the exact failure it was added to prevent (D-109).

    **Rows marked `RESERVED` are excluded**, because they are the one legitimate way for the two to
    differ: a number claimed on a branch whose ADR is not written yet. Requiring exact equality made
    the documented convention impossible to follow — see `_RESERVED`.
    """
    on_disk, written = _adr_ids(), _written_ids()
    missing = [adr for adr in on_disk if adr not in set(written)]
    extra = [adr for adr in written if adr not in set(on_disk)]
    assert not missing, f"in docs/decisions/ but not listed in its README.md: {missing}"
    assert not extra, (
        f"listed in docs/decisions/README.md but no such file: {extra}. If the ADR is still to be "
        f"written, mark the row '{_RESERVED} — …' so the number stays claimed."
    )
    assert on_disk == written, (
        "docs/decisions/README.md lists the same ids as the files beside it but in a different "
        "order; the ledger is ascending, mirroring the record"
    )


def test_every_written_row_links_to_its_file() -> None:
    """A ledger row is only useful if it reaches the ADR — the index is how a reader navigates."""
    index = _INDEX.read_text("utf-8")
    names = {_adr_id(path): path.name for path in _adr_files()}
    unlinked = [
        adr for adr in _written_ids() if adr in names and f"[{adr}]({names[adr]})" not in index
    ]
    assert not unlinked, (
        f"listed in docs/decisions/README.md without a link to their file: {unlinked}"
    )


def test_a_reserved_number_has_no_adr_yet() -> None:
    """Once the ADR is written the marker comes off, or the ledger stops meaning anything.

    A row left marked `RESERVED` after its ADR has merged would read as an unclaimed number to the
    next author enumerating against `origin/main` — handing out a number already in use, which is
    the collision the ledger exists to prevent.
    """
    written_up = sorted(set(_reserved_ids()) & set(_adr_ids()))
    assert not written_up, (
        f"still marked {_RESERVED} in docs/decisions/README.md but written: {written_up}; "
        "replace the marker with the ADR's title and a link to its file"
    )


def test_the_index_has_no_duplicate_reservations() -> None:
    """Two branches reserving the same number is exactly the collision this ledger is for.

    Caught here as a one-line conflict rather than after a merge has buried it in an ADR's prose.
    """
    duplicates = sorted(adr for adr, count in Counter(_index_ids()).items() if count > 1)
    assert not duplicates, f"docs/decisions/README.md reserves the same number twice: {duplicates}"


def test_nothing_in_the_record_carries_an_unresolved_conflict_marker() -> None:
    """A `<<<<<<<` left in the record is invisible to every other check here, and once was.

    The id checks parse filenames, headings and `| D-NNN |` rows, so three marker lines sat in the
    ledger on `main` while every assertion above passed: the rows on both sides of the conflict were
    kept, the ids were fine, and nothing looked at the lines between them. The ledger's whole
    purpose is that "which numbers are taken?" is one listing a human trusts, so a file that still
    shows a half-finished merge undermines the mechanism rather than the data.
    """
    for path in [_INDEX, *_adr_files()]:
        offenders = [
            f"{path.name}:{number}"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "======="
        ]
        assert not offenders, f"unresolved merge conflict markers: {offenders}"


def test_two_adrs_on_one_day_are_distinct_ids() -> None:
    """The property the dated form exists for, asserted rather than described.

    Two sessions writing an ADR on the same day is routine here — it is what produced five
    collisions in a single day under the numbered scheme. If the id were the *date*, the dated form
    would reproduce exactly the failure it replaces; because the id is the whole stem, same-day ADRs
    are distinct and only an identical slug collides — as an add/add conflict on a filename, which
    git reports loudly.
    """
    first = Path("D-2026-07-31-adr-ids-that-cannot-collide.md")
    second = Path("D-2026-07-31-a-different-decision-entirely.md")
    assert _adr_id(first) != _adr_id(second)
    assert _FILENAME.match(first.stem) and _FILENAME.match(second.stem)


def test_a_dated_id_round_trips_filename_heading_and_ledger() -> None:
    """The three places an id appears must agree for the dated form exactly as for the numbered one.

    `_adr_id`, `_HEADING` and `_INDEX_ROW` are three independent parsers; a scheme change that
    taught one of them the new shape and not the others would leave the drift checks passing
    vacuously on dated ADRs while still policing numbered ones.
    """
    stem = "D-2026-07-31-adr-ids-that-cannot-collide"
    assert _adr_id(Path(f"{stem}.md")) == stem
    assert _HEADING.findall(f"# {stem} — ADR ids that cannot collide\n") == [stem]
    row = f"| [{stem}]({stem}.md) | ADR ids that cannot collide |\n"
    # Compared the way `_index_rows` consumes it — the title cell is stripped there, so asserting
    # its exact whitespace would pin a detail no caller depends on.
    assert [(adr, title.strip()) for adr, title in _INDEX_ROW.findall(row)] == [
        (stem, "ADR ids that cannot collide")
    ]


def test_numbered_ids_sort_before_dated_ones_and_stay_numeric() -> None:
    """Record order is defined here, and the ledger is asserted to match it exactly.

    **`D-900` is the case that makes this test mean anything.** Sorting the stems as plain strings
    gives the right answer for every id in the record today — `D-001` … `D-166` are zero-padded, so
    lexicographic order *is* numeric order, and they all begin `D-0`/`D-1`, which precedes
    `D-2025-…`. So a sort key that ignored the two shapes entirely would pass a test built from
    today's ids while being wrong.

    It is wrong from `D-300` onward: `"D-900-…" > "D-2025-…"` as strings, because `'9' > '2'`. A
    numbered ADR would then sort after the dated ones and the ledger-order assertion would start
    failing years from now, for a reason nobody would connect to this function. The first version of
    this test used `D-009`/`D-010` and did not catch that — it survived flattening the key to a
    single lexicographic tuple.
    """
    paths = [
        Path("D-2026-01-02-later.md"),
        Path("D-900-nine-hundred.md"),
        Path("D-2025-12-31-earlier.md"),
        Path("D-009-nine.md"),
    ]
    assert [p.stem for p in sorted(paths, key=_sort_key)] == [
        "D-009-nine",
        "D-900-nine-hundred",
        "D-2025-12-31-earlier",
        "D-2026-01-02-later",
    ]


def test_a_malformed_id_is_still_rejected() -> None:
    """Widening the shape must not widen it to anything — the filename check is a real gate.

    A scheme change is exactly when a validator quietly becomes permissive, so the shapes that were
    invalid before must still be invalid: an uppercase slug, a missing slug, a two-digit month, a
    bare date with no decision in it.
    """
    for bad in ("D-2026-7-31-slug", "D-2026-07-31", "D-999", "D-2026-07-31-Slug", "D-1234-slug"):
        assert not _FILENAME.match(bad), f"{bad} should not be a valid ADR filename"


# ---------------------------------------------------------------------------------------------
# The "By topic" table — the navigational half of this index, and the half nothing checked.
# ---------------------------------------------------------------------------------------------
# The record table above is enforced four ways: unique ids, filename↔heading, ledger↔disk, order.
# The topic table beside it — "where the current decision on a subject is", the one thing a reader
# consults before opening any ADR — was enforced by nothing at all. So it could not go red; it
# could only go wrong, and it did: audited on 2026-09-07 it cited **nothing** newer than
# 2026-08-30, **zero** of the 54 September ADRs appeared in it, and seven rows pointed a reader at
# a decision that had since been reversed (the PR-gate, deleted; the HPC tier, deleted; the
# provider seam, collapsed to one gateway; the compaction arithmetic, rewritten four times). One
# row was worse than stale — it listed D-160 as *absorbed* while five modules in `src/` cite it as
# a live mechanism.
#
# **A bound, not a measurement.** Requiring a topic row per ADR would be wrong: the index's own
# preamble says a subject with exactly one ADR is not listed, and a review sweep is not a subject.
# What is asserted instead is that no ADR newer than the cursor can land *unfiled* — it is either
# cited by a topic row, or named here with the one line saying why it is not a topic. That check
# would have failed 54 times before it was written, which is why it is written.
_TOPIC_CURSOR = "D-2026-09-01"

_NOT_A_TOPIC: dict[str, str] = {
    # Review sweeps. Each is a record of what a set of fresh contexts found across the whole tree,
    # so its subject is the method rather than a part of the system; the fixes it made are filed
    # under the topics they touched.
    "D-2026-09-03-a-guard-that-fails-open-in-its-own-example": "review sweep, not a subject",
    "D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit": "review sweep, not a subject",
    "D-2026-09-04-a-review-of-a-review-finds-the-fixes": "review sweep, not a subject",
    "D-2026-09-04-fifteen-fresh-contexts-over-one-tree": "review sweep, not a subject",
    "D-2026-09-05-four-reviews-of-one-days-measurement": "review sweep, not a subject",
    "D-2026-09-05-six-reviews-of-eight-hours-work": "review sweep, not a subject",
    "D-2026-09-05-a-reader-outlives-its-writer-more-quietly-than-a-writer": (
        "review sweep over the gate deletion, not a subject"
    ),
    "D-2026-09-05-a-reader-with-no-caller-passes-its-own-tests": (
        "review sweep over the gate deletion, not a subject"
    ),
}


def _topic_section() -> str:
    """The `## By topic` section only — the record table below it is a different assertion."""
    index = _INDEX.read_text("utf-8")
    start = index.index("## By topic")
    end = index.index("## Where the record still says")
    return index[start:end]


def _topic_cited_ids() -> set[str]:
    """Every ADR id any topic row names, in either column."""
    return set(re.findall(rf"{_DATED}|{_NUMBERED}", _topic_section()))


def test_no_recent_adr_lands_unfiled_by_the_topic_table() -> None:
    """Every ADR on or after `_TOPIC_CURSOR` is either a topic row's subject or declared not one.

    The topic table is the reader's entry point over 500+ decisions, and it is the only part of
    this index that a *new* ADR can silently invalidate: writing one changes what is current about
    a subject without touching any row that says so. Nothing here can force a row to be *right* —
    that is a review matter, like an ADR's prose. What it can force is that the question was asked.
    """
    unfiled = sorted(
        adr
        for adr in _adr_ids()
        if adr >= _TOPIC_CURSOR and adr not in _topic_cited_ids() and adr not in _NOT_A_TOPIC
    )
    assert not unfiled, (
        f"{len(unfiled)} ADR(s) newer than {_TOPIC_CURSOR} are cited by no row of the 'By topic' "
        f"table in docs/decisions/README.md and are not declared in _NOT_A_TOPIC: {unfiled}. "
        "Either put the ADR in the row for its subject (updating what that row says to read now), "
        "or add it here with the one line saying why it is not a subject."
    )


def test_no_declared_non_topic_is_stale() -> None:
    """`_NOT_A_TOPIC` may not outlive its ADRs, and may not shadow a row that now cites one.

    The same ratchet shape as `_KNOWN_PRIVATE_IMPORTS` in `tests/test_third_party_layering.py`: a
    declared exception that is never re-checked becomes a second, unread index. A row added for an
    ADR later must delete its line here, and an ADR that is renamed or removed must too.
    """
    on_disk = set(_adr_ids())
    missing = sorted(adr for adr in _NOT_A_TOPIC if adr not in on_disk)
    assert not missing, f"_NOT_A_TOPIC names ADRs that do not exist: {missing}"
    filed_anyway = sorted(adr for adr in _NOT_A_TOPIC if adr in _topic_cited_ids())
    assert not filed_anyway, (
        f"declared not-a-topic but now cited by a topic row: {filed_anyway}; delete the row here"
    )


def test_the_topic_cursor_is_not_ahead_of_the_record() -> None:
    """The cursor may only move backwards in time, i.e. cover *more* of the record, never less.

    A cursor set past the newest ADR would make the check above vacuous while still passing — the
    failure mode of every ratchet, and the reason `tests/test_context_floor.py` states its ceiling
    rather than deriving one. Asserting that at least one ADR is in scope is the cheap half; the
    real bound is that this constant is a date somebody has to raise deliberately, in a diff.
    """
    in_scope = [adr for adr in _adr_ids() if adr >= _TOPIC_CURSOR]
    assert in_scope, (
        f"_TOPIC_CURSOR = {_TOPIC_CURSOR} is newer than every ADR on disk, so the ratchet asserts "
        "nothing. Lower it, or delete it and say in a commit why the table needs no bound."
    )


# ---------------------------------------------------------------------------------------------
# "What keeps it true" — an ADR's list of guards, resolved against the suite.
# ---------------------------------------------------------------------------------------------
# An ADR's closing section names the tests that hold its decision in place. That list is the
# load-bearing half of the document — the part a later session reads to find out whether a claim is
# still enforced — and it was the only half nothing checked, so a rename retired a citation in
# silence. Audited on 2026-09-07: 200 distinct `test_*` names are claimed across the record and 38
# resolve to nothing, three of them silent renames that leave a merged ADR naming a live guard by a
# dead name (`test_the_registry_has_no_duplicate_reservations` → `test_the_index_…`,
# `test_concurrent_first_turns_get_one_migrated_store` → `…_memory_store`,
# `test_registry_holds_exactly_the_inprocess_tools` → `test_registry_holds_the_inprocess_tools_…`).
#
# **A merged ADR is never edited**, so this cannot be fixed by correcting them — the record is
# right about the moment it was written. What it can do is stop the *next* one from being written,
# and put the 38 in one place a reader can see. Same allowlist shape as
# `tests/test_docstring_paths.py::_REMOVED`, and the same reason for the friction: adding a row here
# should cost a sentence.
#
# Scope note: `tests/test_docstring_paths.py` deliberately excludes `docs/decisions/` from its path
# check, because a stale *path* in an ADR is accurate about the past. A `test_*` citation is a
# different claim — "this guarantee is enforced right now, here" — which is why it is checked here
# and there rather than exempted with the paths.
_RETIRED_TEST_CITATIONS: dict[str, str] = {
    # Renamed because the old name described a property the test does not check. It calls
    # `skill_permits` with the *manifest* basis, which is right for a tree-vs-manifest check and
    # wrong as a claim about what a turn can reach — and wave 14 made the production basis the
    # *bound* tools, so a name saying "on the full surface" would now name the one basis this test
    # deliberately does not use.
    "test_no_shipped_skill_is_orphaned_on_the_full_surface": (
        "renamed `test_no_shipped_skill_declares_only_tools_no_manifest_advertises` "
        "(tests/test_skill_access.py)"
    ),
    # Renamed. The guard is live and the ADR names it by a name nothing answers to.
    "test_the_registry_has_no_duplicate_reservations": (
        "renamed `test_the_index_has_no_duplicate_reservations`, in this file"
    ),
    "test_concurrent_first_turns_get_one_migrated_store": (
        "renamed `test_concurrent_first_turns_get_one_migrated_memory_store` "
        "(tests/test_scratchpad.py)"
    ),
    "test_registry_holds_exactly_the_inprocess_tools": (
        "renamed `test_registry_holds_the_inprocess_tools_and_only_generated_launchers_besides` "
        "(tests/test_tool_registry.py)"
    ),
    "test_chart_config_keys_are_real_settings": (
        "renamed `test_chart_config_keys_have_a_consumer` (tests/test_helm_chart.py)"
    ),
    "test_image_ships_every_first_party_package": (
        "renamed `test_image_ships_the_first_party_source_tree` (tests/test_deploy_chart.py)"
    ),
    "test_a_rejected_statement_reaches_the_caller_without_the_query_in_it": (
        "nearest live guard is `test_a_rejected_statement_is_not_retryable_and_quotes_nothing` "
        "(tests/test_databricks_warehouse.py); not obviously the same assertion, which is why it "
        "is listed rather than treated as a rename"
    ),
    "test_no_connector_bundle_can_reach_the_pr_gate_itself": (
        "renamed `test_no_connector_bundle_can_reach_the_note_write_path` when the gate went"
    ),
    # The subject left this repository. `D-2026-08-16-the-physics-leaves-the-cache-stays` moved the
    # calculators to `Chemclaw3-mcp`, and `D-2026-08-26-semiempirical-is-the-whole-tier` deleted the
    # HPC/DFT tier; `D-2026-08-15-safety-is-a-tool-not-a-gate` moved the screen the same way.
    "test_a_complex_key_names_both_programs_that_produced_it": (
        "the physics left; the guard is in Chemclaw3-mcp"
    ),
    "test_a_crest_search_refuses_by_name_when_the_binary_is_absent": "the physics left",
    "test_both_backends_reach_the_same_minimum": "the physics left",
    "test_deriving_a_key_runs_no_scf": "the physics left",
    "test_in_sample_pkah_errors_are_far_below_the_acid_calibrations": "the physics left",
    "test_predicted_pkah_ranks_aromatic_bases_correctly": "the physics left",
    "test_the_three_optimizations_run_on_the_backend_the_key_names": "the physics left",
    "test_the_bundle_has_no_way_to_write_the_note_itself": (
        "rewritten by D-2026-08-26-semiempirical-is-the-whole-tier"
    ),
    "test_an_ordinary_combination_is_not_flagged": (
        "the safety screen left (D-2026-08-15-safety-is-a-tool-not-a-gate)"
    ),
    # The specialist team and the challenge panel were deleted whole.
    "test_a_delegated_turn_announces_the_handoff_and_the_hand_back": (
        "no specialist team (D-2026-08-15-a-capability-that-ships-off-is-not-a-capability)"
    ),
    "test_a_specialists_events_are_attributed_to_the_specialist_not_to_the_tool_node": (
        "no specialist team"
    ),
    "test_the_agent_attribution_is_read_from_the_subgraph_namespace": "no specialist team",
    "test_the_specialists_own_output_falls_between_its_handoff_and_its_hand_back": (
        "no specialist team"
    ),
    "test_the_main_agent_records_an_empty_specialist_and_nothing_else_changes": (
        "no specialist team"
    ),
    # The PR-gate and its review queue.
    "test_a_non_reviewer_sees_only_their_own_proposals": (
        "the gate is deleted (D-2026-09-05-the-gate-is-deleted-not-dormant)"
    ),
    # Absence tests that became presence tests, and a provider that became one gateway.
    "test_nothing_in_the_tree_writes_the_agent_column": (
        "inverted by D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail, which is the "
        "outcome that absence test demanded"
    ),
    "test_the_audit_row_records_an_empty_agent_and_nothing_else_changes": "same inversion",
    "test_the_anthropic_payload_is_why_that_refusal_exists": (
        "the provider concept is deleted (D-2026-09-04-a-gateway-is-the-only-provider)"
    ),
    "test_no_module_level_call_dials_the_provider_at_collection": "same collapse",
    # Deleted deliberately, and each deletion is stated in an ADR rather than inferred here.
    "test_the_same_series_at_one_point_steps_is_plateaued": (
        "D-2026-08-05-a-gain-is-measured-from-the-last-gain says it is replaced by its own opposite"
    ),
    "test_the_sync_path_announces_what_the_async_path_announces": (
        "D-2026-08-30-a-review-of-the-review records the deletion; "
        "tests/test_agent_observability_model.py carries the note"
    ),
    "test_the_grandfathered_set_can_only_shrink": (
        "deleted by D-2026-08-27-eighteen-names-for-a-primitive-set"
    ),
    "test_the_counter_counts_attempts_while_the_stream_counts_losses": (
        "the mechanism is replaced by "
        "D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure"
    ),
    "test_the_private_ca_client_is_built_once_per_process": (
        "named by D-2026-09-05-four-reviews-of-one-days-measurement inside a fix it discarded"
    ),
    "test_the_newest_decision_is_the_last_one": (
        "this file's own earlier assertion, replaced by "
        "test_the_index_lists_exactly_the_decisions_on_disk"
    ),
    # Module names written as function names. They read as a `test_*` citation and are not one.
    "test_agent": "a module name from D-036, and no module of that name exists either",
    "test_audit_chain": "a module name; deleted with the hash chain (D-2026-08-14)",
    "test_embedding_provider": "a module name; no such file",
    "test_interaction_tools": "a module name; no such file",
    "test_kg_validate": "a module name; no such file",
    "test_mcp_transport": (
        "a module name; the transport guard is tests/test_connector_transport.py"
    ),
}

_TEST_CITATION = re.compile(r"`(test_[a-z0-9_]+)`")


def _defined_test_names() -> set[str]:
    """Every `test_*` function the suite defines, plus every `test_*.py` module stem.

    Both are legitimate things for an ADR to cite by name, and the stems have to be included or a
    sentence naming `tests/test_authz.py` as `test_authz` reads as a dangling function.
    """
    import ast

    tests = _DECISIONS.parents[1] / "tests"
    names = {path.stem for path in tests.rglob("test_*.py")}
    for path in tests.rglob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"), filename=str(path))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test_"
            ):
                names.add(node.name)
    return names


def test_every_test_an_adr_names_still_exists() -> None:
    """A guard an ADR cites by name resolves against the suite, or is declared retired above.

    The "What keeps it true" list is what makes an ADR checkable instead of persuasive. A citation
    that resolves to nothing looks identical to one that resolves — it reads as authoritative while
    pointing at nothing, which is the same failure `test_every_adr_id_is_unique` exists for one
    level down.
    """
    defined = _defined_test_names()
    dangling = sorted(
        {
            name
            for path in _adr_files()
            for name in _TEST_CITATION.findall(path.read_text("utf-8"))
            if name not in defined and name not in _RETIRED_TEST_CITATIONS
        }
    )
    assert not dangling, (
        f"{len(dangling)} test name(s) cited in docs/decisions/ resolve to nothing: {dangling}. "
        "A merged ADR is never edited, so the fix is one of two things: rename the test back, or "
        "add the name to _RETIRED_TEST_CITATIONS with the line saying what replaced it."
    )


def test_no_retired_test_citation_is_stale() -> None:
    """A name that comes back — restored, or re-used by a new test — loses its row.

    Without this the allowlist becomes a second, unread index of names nobody re-checks, which is
    the failure mode the list is meant to end rather than reproduce.
    """
    defined = _defined_test_names()
    resurrected = sorted(name for name in _RETIRED_TEST_CITATIONS if name in defined)
    assert not resurrected, (
        f"declared retired but defined in the suite again: {resurrected}; delete the row"
    )
