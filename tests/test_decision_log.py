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

import pytest

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
_TOPIC_CURSOR = "D-2026-08-31"

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
    # A plan correction: two audit findings retracted because the code had already argued each of
    # them shut. Its subject is a wave of work that will not happen, so there is nothing for a
    # reader of any topic to be sent to it for.
    "D-2026-09-14-two-gaps-the-code-had-already-argued-shut": (
        "retracts two planned items, not a subject"
    ),
    # ---------------------------------------------------------------------------------------
    # The arrears, filed. Everything below is *older* than the cursor and was counted by
    # `_UNFILED_ARREARS_ALLOWANCE` until the record was triaged one ADR at a time: 38 went into
    # the row for their subject, 18 more into five new rows the table had never had, and these
    # are what is left — a defect fix, a review sweep, a one-off tuning, a decision whose subject
    # has exactly one ADR and is found by the record table below. Each line says what the ADR
    # settled, because a placeholder here would make this a second unread index rather than a
    # declaration; `test_no_declared_non_topic_is_stale` is what keeps a line from outliving its
    # ADR or shadowing a row that has since taken it.
    "D-001": (
        "chooses Python >= 3.11 as the runtime before any code existed; one decision with no "
        "successor"
    ),
    "D-023": (
        "a phase-era positioning statement (the agent composes every tool; integrations stay "
        "dumb) whose concrete move was exposing the fingerprint and evidence sweeps as agent "
        "tools, on a surface the LangGraph rebuild replaced"
    ),
    "D-024": (
        "tells the agent to compute properties unprompted and exposes BoFire as a turn-time "
        "design tool; an instruction change and a tool exposure"
    ),
    "D-030": (
        "the Temporal retry and error-classification hardening from a six-pass review: bounded "
        "bad-data retries, git-ref-safe slugs, git timeouts, cache keys"
    ),
    "D-036": (
        "that review's low-severity cleanups: a drifted tool name, a duplicated wikilink regex, "
        "scattered hashing"
    ),
    "D-037": (
        "closes the tooling gaps that let regressions past the local gate: coverage, one mypy "
        "scope, worker tests, preflight, skill-validate"
    ),
    "D-051": (
        "the F4-F7 adversarial review; its sharpest finding was a mock-derived HPC poll timeout "
        "that would have killed every real run"
    ),
    "D-058": (
        "proves the MAF harness loop against a scripted chat client and closes the awaiting-todo "
        "deferral; the loop it exercised no longer exists"
    ),
    "D-065": (
        "the F10 post-implementation review; its headline was a verifier_confidence_threshold "
        "defined and never read"
    ),
    "D-067": (
        "makes an unauthenticated, network-exposed startup refuse to boot; the refusal set that "
        "grew from it is stated wholesale by D-2026-09-04-the-configurations-this-tree-now- "
        "refuses-to-start-in"
    ),
    "D-069": (
        "puts an OS advisory flock on the PR-gate's shared checkout so two processes cannot "
        "corrupt each other's note branches; that gate and its checkout are deleted"
    ),
    "D-071": (
        "two replay-correctness fixes: the fan-out limit captured through a local activity, and "
        "an idempotency key on session events"
    ),
    "D-072": (
        "the CHECKMATE 2026-07 campaign record: 13 reviewers, 73 findings, 50 confirmed after "
        "adversarial re-check"
    ),
    "D-073": (
        "the adversarial diff pass over that campaign, finding nine defects the campaign itself "
        "introduced or left"
    ),
    "D-075": (
        "a MAF-era extensibility pass replacing a hardcoded tool list with a @tool registry and "
        "adding the AgentProfile seam"
    ),
    "D-081": (
        "the last three config-extensibility items: an MCP transport union, a skill manifest and "
        "enable-list, a config idiom rule; the transport union was replaced by the connector "
        "seam"
    ),
    "D-083": (
        "phase F11 waves 0-3, a capability-gap sweep landing deployment, reachability and "
        "chemistry fixes together"
    ),
    "D-084": (
        "phase F11 waves 3-4: the operational surfaces the system could not expose, and the "
        "knowledge model reasoning about itself"
    ),
    "D-085": (
        "phase F11's five blocked items, each decided in place and recorded in the module that "
        "embodies it"
    ),
    "D-086": (
        "the first reconciliation with main (PRs #17-#20): which of two hazard screens, event "
        "sinks and tool registries won"
    ),
    "D-087": (
        "the second reconciliation with main (PR #21), taking main's MCP transport union over "
        "this branch's validator"
    ),
    "D-093": "the fan-out child that suspended as a task failure, hanging CI for six hours a run",
    "D-094": (
        "CI's kg-validate failing on a Replit-only knowledge symlink that resolves on no other "
        "checkout"
    ),
    "D-105": (
        "the fourth reconciliation with main (PR #28), where the restored tree met the xTB layer"
    ),
    "D-106": (
        "a heavy read of the xTB layer finding five defects a green suite missed, three of them "
        "contradicted by their own docstring"
    ),
    "D-107": (
        "the fifth reconciliation with main (PR #31): a unit boundary and a sign, both silent, "
        "each existing only in the combination"
    ),
    "D-114": (
        "the sixth reconciliation, where the xTB layer met the connector seam and two branches "
        "proved the same boundary twice"
    ),
    "D-116": (
        "the seventh reconciliation (PR #30) and the two capabilities a modify/delete merge "
        "silently restored"
    ),
    "D-130": (
        "turn teardown ran inside a cancelled task, so its cleanup had to be shielded to happen "
        "at all"
    ),
    "D-131": (
        "the connector health probe followed the address override and probed the wrong endpoint, "
        "so a killed connector and an unprobed one read alike"
    ),
    "D-136": (
        "three shipped configuration defaults that had never been executed, and the class of "
        "defect a suite of fakes cannot see"
    ),
    "D-138": (
        "fifty expert questions asked against the running stack; five defects, four invisible to "
        "an otherwise thorough suite"
    ),
    "D-141": (
        "two facts that stopped at a process boundary: a session's profile on rehydration, and "
        "the turn's correlation id"
    ),
    "D-142": (
        "the chart parity check that never read templates/config.yaml, and two guards that were "
        "off in the one deployment needing them"
    ),
    "D-143": (
        "nothing in deploy/ scraped /metrics in any deployment, and the durable history is never "
        "compacted"
    ),
    "D-155": (
        "what the never-run half of the system did the first time it ran, closing each defect's "
        "class at the gate that should have caught it"
    ),
    "D-162": (
        "an audit of whether the record can propose tomorrow's experiment without BO; what it "
        "found missing was sequence-awareness in the campaign memory"
    ),
    "D-2026-08-01-a-cheap-request-is-still-a-request": (
        "installs the three request-admission bounds the front door lacked: uvicorn connection "
        "flags, a body cap, and a per-principal request budget"
    ),
    "D-2026-08-01-a-gate-that-leaks-on-the-failure-path": (
        "three PR-gate defects visible only on the failure path, including a checkout never "
        "restored; that gate is deleted"
    ),
    "D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down": (
        "declares service_fleet_replicas so the per-process turn cap can be multiplied out and "
        "checked against the shared endpoint"
    ),
    "D-2026-08-01-a-rule-that-counts-cannot-be-a-chain": (
        "gives a structural safety rule a min_matches count, so polynitro-aromatic means what "
        "the word means"
    ),
    "D-2026-08-01-a-running-job-has-no-owner": (
        "adds the job routes a chemist had no way to reach, and makes the cancel an operator "
        "action rather than an owner's"
    ),
    "D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment": (
        "registers three autonomy metrics over scripted transcripts and says plainly that they "
        "gate the harness rather than the model"
    ),
    "D-2026-08-01-every-process-carries-its-own-witness": (
        "gives every worker and connector process the same three routes and a PodMonitor, "
        "because only the front door was scraped"
    ),
    "D-2026-08-01-symmetry-is-an-input-not-a-default": (
        "deletes a spurious factor of two in the linear rotational partition function and makes "
        "the symmetry number a per-species input"
    ),
    "D-2026-08-01-the-agent-slot-that-changed-no-bits": (
        "the agent-slot move DRFP could not see: solvents and catalysts are excluded from the "
        "fingerprinted string rather than relocated within it"
    ),
    "D-2026-08-01-the-cap-reports-itself": (
        "makes the MAF loop cap report the stop it took so runaway_rate measured runaways rather "
        "than unchecked todos; that middleware is gone"
    ),
    "D-2026-08-02-a-solvent-charge-is-a-volume": (
        "gives the charge table solvents/volumes so a solvent charge can be expressed the way a "
        "chemist writes one"
    ),
    "D-2026-08-02-grounding-is-what-this-turn-saw": (
        "scores a turn's answer against that turn's own tool results rather than against the "
        "graph, after a live run measured 22-46% fabrication"
    ),
    "D-2026-08-02-shipped-is-not-reachable": (
        "the capability audit's finding that 57% of user stories already had machinery behind "
        "them and the last mile was missing; leaves a review rule, not a subject"
    ),
    "D-2026-08-02-work-repeated-every-time-for-no-reason": (
        "two costs proportional to the whole corpus paid on every run; reindex_notes becomes "
        "incremental off a per-note stat fingerprint"
    ),
    "D-2026-08-03-the-refactor-closes-what-it-measured": (
        "the closing record of the ~24-package grand refactor, re-measured against its own baseline"
    ),
    "D-2026-08-04-a-lane-that-only-runs-where-docker-runs": (
        "builds the scripted live lane in two stages so it runs with or without a Docker daemon"
    ),
    "D-2026-08-04-a-limit-across-parameters-is-not-a-bound": (
        "gives the optimization problem one LinearConstraint, since a bound per parameter admits "
        "the corner a constraint forbids"
    ),
    "D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with": (
        "makes assay_noise a required argument of the plateau reading, because a plateau test "
        "that supplies its own noise fabricates the answer"
    ),
    "D-2026-08-04-a-screen-may-hold-a-continuous-factor-at-its-bounds": (
        "admits a continuous factor to a screening design held at its two bounds, and makes the "
        "design name every factor it collapsed"
    ),
    "D-2026-08-04-a-trade-off-has-no-single-best-point": (
        "the optimization problem takes a list of objectives, with the single-objective spelling "
        "accepted permanently"
    ),
    "D-2026-08-04-the-model-can-be-asked-not-only-obeyed": (
        "one fitted surrogate serves propose, predict and cross-validate, so a reported fit "
        "quality describes the model the recommendation came from"
    ),
    "D-2026-08-05-a-declaration-outliving-what-it-describes": (
        "six BO declarations that no longer described what they declared, found by two reviewers "
        "after the five waves merged"
    ),
    "D-2026-08-05-a-gain-is-measured-from-the-last-gain": (
        "the stagnation counter measures distance from the last real gain rather than from the "
        "last run"
    ),
    "D-2026-08-05-a-score-reported-more-precisely-than-it-repeats": (
        "a defaulted CV fold count adapts to the run count while a caller-stated one is still "
        "refused, and the result records which was used"
    ),
    "D-2026-08-05-a-sweep-that-commits-once": (
        "retention commits per session, caps the batch and reports the remainder, so a failing "
        "sweep stops early instead of doing nothing"
    ),
    "D-2026-08-05-a-trend-needs-a-tail": (
        "a soak trend claim needs two fits and a tail; growth with no tail long enough to fit is "
        "reported as too short to say"
    ),
    "D-2026-08-05-one-rule-in-three-places-is-three-rules": (
        "collapses rules the engine stated more than once - one plan emitter, one predicate - "
        "after two of them had already produced observable bugs"
    ),
    "D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without": (
        "/readyz probes Postgres under the postgres session store, on its own two-second budget "
        "rather than the statement timeout"
    ),
    "D-2026-08-05-three-searches-that-disagreed-about-one-note": (
        "three haystacks over one note collapsed into one searchable text owned by layer 4"
    ),
    "D-2026-08-06-a-flag-is-a-signal-not-an-inventory": (
        "a note's conflict flag carries its widest disagreements worst-first and capped, rather "
        "than enumerating every pair"
    ),
    "D-2026-08-06-a-gate-that-names-nothing": (
        "the one core durable launcher that escaped the manifest-derived expensive-action gate; "
        "the second occurrence of one shape, not a second decision"
    ),
    "D-2026-08-06-a-swallowed-write-reported-as-a-store": (
        "three tools that told a chemist their data was kept when nothing was configured to keep it"
    ),
    "D-2026-08-06-a-tool-cannot-say-it-has-nothing-twice": (
        "refuses an identical tool call repeated within one turn, and argues why refusing beats "
        "serving the first call's cached result"
    ),
    "D-2026-08-06-the-caller-chooses-the-kid-not-the-workload": (
        "PyJWKClientError is neither InvalidTokenError nor AuthError, so a token naming an "
        "unknown kid escaped every handler"
    ),
    "D-2026-08-06-the-memo-already-carried-the-actor": (
        "a durable BO campaign reported as missing because the resume path never read the actor "
        "the run's memo already carried"
    ),
    "D-2026-08-06-the-method-decides-which-solvents-exist": (
        "the ALPB solvent set is a constant probed out of the library, and a durable job naming "
        "a solvent outside it is refused at launch"
    ),
    "D-2026-08-07-a-manifest-must-say-who-may-read-it": (
        "the security family of the mounted-share review: a data-source manifest must state "
        "required_roles or public, because omission is not a decision"
    ),
    "D-2026-08-07-one-bad-file-must-not-stop-the-corpus": (
        "the availability family of the same review: one net at the parse boundary, so one "
        "malformed file cannot stop the corpus"
    ),
    "D-2026-08-07-the-mark-means-observed-not-processed": (
        "the data-loss family of the same review: the sweep restamps everything the walk "
        "observed, so a file that was refused is not deleted"
    ),
    "D-2026-08-08-a-prefix-the-documents-never-carried": (
        "moves the group-role prefix to the module that owns the role vocabulary, and checks the "
        "prose that teaches it"
    ),
    "D-2026-08-08-a-served-tool-is-a-reachable-tool": (
        "deletes the two index_* tools and makes connector-validate enumerate a bundle's live "
        "tool set; what that validator can reach is restated by D-2026-08-29-connector-validate- "
        "never-dials-a-server"
    ),
    "D-2026-08-08-a-slot-lives-as-long-as-its-response": (
        "five front-door admission-accounting defects; the stream slot is released by the "
        "response rather than by the generator"
    ),
    "D-2026-08-08-a-source-is-named-by-its-folder-not-by-its-half": (
        "the registry passes the manifest name to every retrieve half, so a source's identity is "
        "the folder the deployment enabled"
    ),
    "D-2026-08-08-a-survivor-is-a-hypothesis": (
        "the first mutation run over the seven declared modules: fixes the selection, closes the "
        "genuine gaps, argues the equivalent survivors"
    ),
    "D-2026-08-08-a-test-that-survives-the-mutation-it-names": (
        "nine tests that raised coverage without constraining behaviour, each closed by a test "
        "shown to fail against the exact surviving mutation"
    ),
    "D-2026-08-08-the-inventory-that-vouched-for-itself": (
        "seven claims in CLAUDE.md, ARCHITECTURE.md and four docstrings re-measured and found "
        "false; two of them became tests"
    ),
    "D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one": (
        "a stored tool call finds its blob by content address rather than by a join, and the "
        "transcript checks a ref before advertising it"
    ),
    "D-2026-08-09-a-hand-written-list-of-columns-drifts": (
        "seven review findings against the offboarding and data-source naming work, all "
        "reproduced before being fixed"
    ),
    "D-2026-08-09-a-preview-is-not-a-result": (
        "the tool-result event carries a content-addressed reference rather than a payload, and "
        "a surface fetches the one result it chose to render"
    ),
    "D-2026-08-09-a-scope-that-matches-no-point": (
        "the eligibility scope stayed at the document while the vector group moved to the "
        "cutting, so the scope matched no point"
    ),
    "D-2026-08-09-a-twin-rule-is-one-string": (
        "a hazard motif spelled twice - as a structural rule and as a pair arm - is spelled once "
        "and pinned character-identical"
    ),
    "D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate": (
        "the announcer sat innermost in the tool chain, so every middleware that refuses raised "
        "outside it and no governance refusal was ever announced"
    ),
    "D-2026-08-12-a-review-the-migration-did-not-get": (
        "what 181 reviewers found across 16 domain lanes in a migration that shipped green"
    ),
    "D-2026-08-13-the-guard-must-not-refuse-a-dependency-bump": (
        "narrows the checkpoint stamp to the channels this repository declares, so an upstream "
        "channel moving cannot refuse a thread"
    ),
    "D-2026-08-14-two-http-stacks-is-the-price-of-the-openai-major": (
        "a dependency sweep taking fifteen in-range bumps and three majors, and the one cap that "
        "stays"
    ),
    "D-2026-08-16-a-cache-that-lets-every-caller-miss-together": (
        "six findings in layer 4; a per-directory re-entrant lock so eight cold builders share "
        "one parse and one assembly"
    ),
    "D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs": (
        "every workflow on the job path declares its failure types, so a raised exception fails "
        "the run instead of parking it forever"
    ),
    "D-2026-08-16-a-key-the-caller-cannot-see-is-a-key-the-caller-can-poison": (
        "the defect class carrying out the calc split turned up, which the design ADR is blind "
        "to by construction"
    ),
    "D-2026-08-16-an-announcement-is-not-a-failure": (
        "eight small backlog rows closed together, two of which were one mistake made in two places"
    ),
    "D-2026-08-16-arithmetic-about-a-loop-is-derived-not-configured": (
        "two HPC timing relations become derived properties and one startup refusal is retired; "
        "the tier they timed has since been deleted"
    ),
    "D-2026-08-16-one-tables-failure-should-not-starve-the-rest": (
        "a four-pass database review's six fixes, plus three findings left for a decision"
    ),
    "D-2026-08-16-the-handshake-already-says-which-build-answered": (
        "the trail records the MCP server's build from the initialize() handshake, beside the "
        "orchestrator's revision"
    ),
    "D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two": (
        "the four-repo lane started two of five fleet servers; leaves the rule that /readyz is a "
        "connector probe, not a dependency probe"
    ),
    "D-2026-08-17-a-workflow-type-is-a-launch-contract-not-a-durability-leak": (
        "settles that an agent-layer module naming a core-queue workflow type is the thin "
        "adapter D-002 asks for, since it stores no durable state"
    ),
    "D-2026-08-18-a-corpus-is-not-reachable-because-it-is-on-disk": (
        "make live-data checks the seeded corpus by value against the published tables, because "
        "a count cannot say which records arrived"
    ),
    "D-2026-08-20-a-networkpolicy-selects-peers-not-paths": (
        "four self-hosted bundles that served /mcp with auth mode none get bearer auth and their "
        "own token_env"
    ),
    "D-2026-08-20-a-ui-that-cannot-authenticate-is-not-a-fallback": (
        "three smaller identity-audit findings, the largest a bundled chat UI containing no "
        "Authorization header at all"
    ),
    "D-2026-08-25-a-chunk-cap-is-not-a-context-budget": (
        "gives the evidence sweep a character bound beside its chunk count, because forty chunks "
        "means a different size per source"
    ),
    "D-2026-08-25-the-number-was-measured-on-a-path-production-does-not-use": (
        "corrects the condenser's measured saving, which was taken on a serialization path no "
        "model receives"
    ),
    "D-2026-08-25-the-structure-is-discarded-at-the-note-boundary": (
        "a protocol reaches the model whole, addressed by exactly the citation string it is "
        "already cited as"
    ),
    "D-2026-08-26-a-barrier-is-a-difference-between-two-numbers-measured-the-same-way": (
        "eight defects found reviewing the merged rotational profile, two of which carry a rule "
        "about comparing two energies"
    ),
    "D-2026-08-26-a-guard-that-runs-at-collection-guards-nothing": (
        "eight findings against the merged field-benchmark change, whose shared shape is a "
        "control that runs at the wrong moment and inverts"
    ),
    "D-2026-08-26-a-pka-is-a-macrostate-not-a-microstate": (
        "adds the ensemble pKa as a composite here rather than a server tool, because its key "
        "would name the microstate the search settles on"
    ),
    "D-2026-08-26-a-projector-per-shape-the-loop-produces": (
        "four projectors, one per multi-step result shape, so those jobs' results become "
        "queryable records"
    ),
    "D-2026-08-26-a-release-is-a-descriptor-and-a-target": (
        "the Jenkins delivery design across four repositories, accepted and unrun against a "
        "cluster this environment does not have"
    ),
    "D-2026-08-26-a-renderer-that-places-a-cell-guarantees-it-stays-one": (
        "the shared table renderer escapes the pipe and collapses whitespace, since both callers "
        "had the exposure through different fields"
    ),
    "D-2026-08-26-a-sampler-nobody-ships-is-a-refusal-with-a-manual": (
        "ships pinned CREST and xtb binaries in the calc image, having found three of its four "
        "searches broken"
    ),
    "D-2026-08-26-a-solvent-is-an-argument-not-a-job": (
        "adds a species ranking that fans out over media the way a reaction comparison already "
        "did, with gas phase as the reference"
    ),
    "D-2026-08-26-a-tool-name-is-one-capability-or-it-is-neither": (
        "the first statement that one declared name is one capability across a bundle's endpoint "
        "and job halves; the rule now reads across every namespace in D-2026-09-04-a-name-is- "
        "one-capability-across-every-namespace"
    ),
    "D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire": (
        "what the adversarial review of the merged GFN multi-step work found: four of seven "
        "shipped tools wrong under a green CI"
    ),
    "D-2026-08-26-a-transcription-is-keyed-by-its-source": (
        "a reaction record's row identity is the registry source name plus the entry id, since "
        "the rendered provenance string is not stable"
    ),
    "D-2026-08-26-an-atom-index-is-not-a-name": (
        "reactivity descriptors are addressed by a named site rather than an atom index, and the "
        "binary refuses what it cannot do"
    ),
    "D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution": (
        "deletes the specialist contextvar nothing ever set, keeping the column, the event and "
        "the rule it carried"
    ),
    "D-2026-08-26-an-empty-allow-list-is-not-an-allow-list": (
        "an endpoint must declare at least one tool and the allow-list becomes total; the tool- "
        "list rule is restated by D-2026-09-12-a-tool-list-is-a-name-space-whichever-argument- "
        "it-arrives-on"
    ),
    "D-2026-08-26-silence-is-not-a-successful-run": (
        "outcome_class becomes optional so a source with no status column stops reporting every "
        "record as a success"
    ),
    "D-2026-08-27-a-bound-that-multiplies-and-a-record-that-survives-the-cancel": (
        "a BoFire audit finding that a ceiling on rounds is not a ceiling on cost, since a round "
        "costs a batch of evaluations"
    ),
    "D-2026-08-27-a-bundle-may-lower-its-own-ceiling": (
        "gives a job spec an optional timeout that may lower the deployment's execution ceiling "
        "and may never raise it"
    ),
    "D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step": (
        "the stored-message conversion keeps the original column, because three documents called "
        "it safe and each meant a different property"
    ),
    "D-2026-08-27-a-disconnect-is-a-detach-not-a-stop": (
        "a turn runs on its own pump task and a dropped SSE reader detaches rather than "
        "cancelling; carried forward by D-2026-09-14-a-turn-outlives-its-request-already-and- "
        "nothing-can-pick-it-up"
    ),
    "D-2026-08-27-a-free-energy-without-its-standard-state-is-not-a-quantity": (
        "a solution-phase free energy is quoted at 1 mol/L, derived from the phase rather than "
        "configured"
    ),
    "D-2026-08-27-a-gradient-is-the-evidence-a-frequency-set-cannot-carry": (
        "three cross-repository seams verified against both trees, including a Hessian preflight "
        "written on the side that knows the alternatives"
    ),
    "D-2026-08-27-a-job-names-the-step-it-serves": (
        "a launched job is stamped with the plan step it was launched for, because editing the "
        "todo would revoke the approval that authorized it"
    ),
    "D-2026-08-27-a-job-that-fails-leaves-no-row": (
        "a failed durable run emitted nothing; one worker interceptor now carries the "
        "obligations each activity owed"
    ),
    "D-2026-08-27-a-number-with-no-name-is-not-a-measurement": (
        "adds labelled values and an inline result to the tool-result event, labelled by the "
        "payload's own key path"
    ),
    "D-2026-08-27-a-pass-that-leaves-no-record-cannot-be-said-to-have-run": (
        "instruments the data path at the seams every caller already goes through, one finished "
        "record per ingest pass"
    ),
    "D-2026-08-27-a-per-worker-cap-is-not-a-backend-ceiling": (
        "checks the calculation fleet's worker product against the backend's concurrent-request "
        "ceiling, as the other two budgets already were"
    ),
    "D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it": (
        "fourteen of twenty background workflows declare their failure types and six "
        "deliberately do not, because a Schedule already bounds them"
    ),
    "D-2026-08-27-a-queue-with-no-poller-is-unreachable": (
        "the durable half of a connector gains a probe: whether anything polls the queue its "
        "jobs run on"
    ),
    "D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask": (
        "a refused ingest record becomes a keyed ledger row with first_seen, last_seen and a "
        "count, rather than a warning nobody keeps"
    ),
    "D-2026-08-27-a-request-nobody-recorded-and-a-turn-that-ended-six-ways": (
        "nine places where the front door knew something and kept no record of it, including no "
        "first-party access log"
    ),
    "D-2026-08-27-a-retirement-rides-its-replacement": (
        "a synthesis run produces units pairing a new note with the retirements it replaces, so "
        "neither half can land without the other"
    ),
    "D-2026-08-27-a-session-list-is-a-cursor-and-a-session-is-deletable": (
        "the conversation listing pages by a keyset cursor and a session becomes deletable, "
        "closing a backlog row whose original claim was wrong"
    ),
    "D-2026-08-27-a-session-nobody-can-reopen-is-disposable": (
        "a session-owner row is pruned only when nothing can reopen the session and nothing is "
        "left to reopen into"
    ),
    "D-2026-08-27-a-solvate-is-not-its-solvent": (
        "fragments are stripped only when exactly one of them is organic, so a solvate or co- "
        "crystal is kept whole"
    ),
    "D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait": (
        "every durable activity gains a schedule-to-start bound from one shared helper, because "
        "a start-to-close timeout does not bound the queue"
    ),
    "D-2026-08-27-a-step-runs-under-the-correlation-id-it-was-launched-with": (
        "a template step binds all three ambients as one bracket; the correlation id was the one "
        "it never stamped"
    ),
    "D-2026-08-27-an-index-must-match-the-sort-it-serves": (
        "adds the observations index matching the sort the open-observations read performs, and "
        "corrects the rationale the old index stated"
    ),
    "D-2026-08-27-count-the-trajectories-before-building-the-distiller": (
        "defines the trajectory census and defers a skill generator until the census reports a "
        "number; a deferral with its trigger"
    ),
    "D-2026-08-27-eighteen-names-for-a-primitive-set": (
        "probes the seventeen names one merge added to the default surface and deletes the "
        "grandfathered list rather than emptying it"
    ),
    "D-2026-08-27-one-lane-starts-the-fleet": (
        "settles which of two live lanes starts a shared fleet server: the one that cannot start "
        "without it"
    ),
    "D-2026-08-27-the-backend-is-told-who-is-asking": (
        "the calculation backend stopped logging any caller; one identity hook is passed into "
        "both of this repository's MCP client paths"
    ),
    "D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken": (
        "a connector known to be down is not dialled again this window; one dict and a "
        "predicate, deliberately not a breaker framework"
    ),
    "D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed": (
        "four ways the PR-gate's record diverged from what git actually held; that gate is deleted"
    ),
    "D-2026-08-27-what-a-second-background-worker-would-race-on": (
        "audits the background queue under two workers, keeps the replica pin at 1 for a reason "
        "that is currently true, and adds no lock"
    ),
    "D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop": (
        "an append-only reaction source keeps a keyset position and every corpus reaction gets a "
        "DRFP row"
    ),
    "D-2026-08-28-a-gate-that-cannot-fire-and-a-rate-with-no-denominator": (
        "the weekly mutation job could not create the label it files under, and its kill rate "
        "had no stated denominator"
    ),
    "D-2026-08-28-a-watermark-that-is-rewritten-has-no-age": (
        "six findings reviewing the feed change after it merged; a cursor is stored when it "
        "moves, and four of that ADR's claims are corrected"
    ),
    "D-2026-08-28-a-workflow-body-cannot-count-itself": (
        "the in-flight job reading is asked of the broker rather than counted by the workflow, "
        "and a failure record never erases a result"
    ),
    "D-2026-08-28-an-erasure-that-cannot-name-what-it-missed": (
        "joins the two registers that bound the durable stores and adds the retained-in-payload "
        "tier they had no way to state"
    ),
    "D-2026-08-28-an-inbox-asks-a-narrower-question-than-a-card": (
        "adds a cross-session listing of plans with no decision recorded; a read over the gate's "
        "existing records, not a change to what it decides"
    ),
    "D-2026-08-28-the-durable-half-has-a-backend-too": (
        "applies the one-lane rule to the calc backend: the lane whose work needs a server is "
        "the lane that starts it"
    ),
    "D-2026-08-28-the-review-of-the-erasure-change-found-three-of-its-own-defects": (
        "four adversarial passes over the merged memory-bounds change, and the two claims it "
        "retracts"
    ),
    "D-2026-08-29-a-bound-derived-twice-is-two-bounds": (
        "the ingest refusal ledger is written where the chunk is known, after the same bound was "
        "derived in two places"
    ),
    "D-2026-08-29-a-check-a-reader-never-sees-is-not-a-check": (
        "six fresh-context reviewers over the prescriptive tier, finding more than two prior "
        "cycles combined"
    ),
    "D-2026-08-29-a-docstring-is-not-a-measurement": (
        "five independent reviews of the spend-cap change, seven defects, each closed by a test "
        "shown to fail on the parent commit"
    ),
    "D-2026-08-29-a-gate-binds-what-the-registry-calls": (
        "datasource-validate bound the name argument for the retrieve half only, so it passed a "
        "config the worker refuses"
    ),
    "D-2026-08-29-a-guard-that-names-one-file-guards-one-file": (
        "the in-tool model-call scan derives its own module set - every module that both defines "
        "a tool and builds a model - instead of naming one file"
    ),
    "D-2026-08-29-a-mirror-is-not-a-plan": (
        "adds the commitments mirror and declines to plan, schedule or level resources here, "
        "because the portfolio tool is the truth"
    ),
    "D-2026-08-29-a-per-read-timeout-is-not-a-budget": (
        "bounds the readiness sweep's three remaining unbounded halves with a wall clock rather "
        "than a timeout keyword"
    ),
    "D-2026-08-29-a-review-with-fresh-context-is-a-different-instrument": (
        "an audit of a merged infrastructure change that retracts two of its factual claims "
        "rather than editing them away"
    ),
    "D-2026-08-29-a-schema-that-cannot-change-is-not-per-turn-work": (
        "derives each in-process tool's schema once per process, because a first-party tool's "
        "schema cannot vary between turns"
    ),
    "D-2026-08-29-a-sign-off-names-a-revision-or-it-names-nothing": (
        "the second adversarial pass over the prescriptive tier: seven more defects and a status "
        "record this tree claimed to keep and did not"
    ),
    "D-2026-08-29-the-review-of-the-prescriptive-tier-found-fifteen-defects": (
        "the first adversarial pass over the merged prescriptive tier, all fifteen defects under "
        "a green suite"
    ),
    "D-2026-08-30-a-review-by-six-strangers-found-thirty-seven-defects": (
        "the fifth review cycle over the prescriptive tier, run by six fresh contexts; 37 "
        "defects, two left as backlog rows"
    ),
    "D-2026-08-30-a-review-of-the-review": (
        "three fresh-context reviewers over the change that replaced the mechanism, each told to "
        "run the code rather than read the prose"
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


def test_no_subject_has_two_topic_rows() -> None:
    """One subject, one row — because a reader takes the first row they find as *the* answer.

    `test_the_index_has_no_duplicate_reservations` covers the record table, where a repeated id is
    a collision. Nothing covered the navigational half, and a duplicate there is worse in a quieter
    way: both rows parse, both cite real ADRs, every other assertion in this file passes, and the
    two copies then drift — one gains a citation the other does not, so which decision a reader is
    told is current depends on which line they stopped at.

    Found by merging: `main` carried two "Publishing & delivery" rows, one of them missing a
    citation the other had, and the conflict that surfaced it was a side effect rather than a
    check. A guard is what makes the next one loud.
    """
    subjects = [
        line.split("|")[1].strip()
        for line in _topic_section().splitlines()
        if line.startswith("| ") and line.count("|") >= 3
    ]
    named = [s for s in subjects if s and not set(s) <= set("- ") and s != "Topic"]
    duplicates = sorted(topic for topic, count in Counter(named).items() if count > 1)

    assert not duplicates, (
        "the By topic table carries two rows for the same subject, so which decision a reader is "
        f"told is current depends on which line they stop at: {duplicates}"
    )


#: How many ADRs *older* than the cursor no topic row mentions, and it is now **zero**: the
#: arrears were triaged one ADR at a time and every one of them is either cited by a topic row or
#: declared in `_NOT_A_TOPIC` with the line saying why it is not a subject. It was an allowance in
#: the shape `SERVED_ELSEWHERE_ALLOWANCE` has while there was something to allow; at zero it is no
#: longer an allowance at all but the exact count, so the only way to raise it is a commit that
#: leaves an ADR unfiled — and that commit is the one this bound exists to turn red.
_UNFILED_ARREARS_ALLOWANCE = 0


def test_the_arrears_of_unfiled_adrs_only_shrink() -> None:
    """Every ADR older than the cursor is cited by a topic row or declared not a subject.

    `_TOPIC_CURSOR` stops a *new* decision landing with this index silent about it, and said
    nothing about the ones already there. Audited, 231 of 680 were cited by no row — so for a third
    of the record nothing told a reader whether the decision is current, while every one of them
    carries `**Status:** accepted`. That is the shape a reader of a superseded ADR gets no warning
    from, and it is why `tests/test_dead_vocabulary.py` exists for the vocabularies where the
    premise died outright. Those 231 have since been filed: 38 into the row for their subject, 18
    into five rows the table had never had, and the rest declared in `_NOT_A_TOPIC`.

    **So the bound is exact rather than an allowance.** It used to carry a second, lower assertion
    — `arrears >= allowance - 25` — whose job was to stop the allowance drifting far above reality
    and quietly ceasing to bind, which is a real failure while an allowance is a non-zero number
    somebody lowered by hand. At zero it cannot happen twice over: a count is never negative, so
    `x >= -25` can never fail and would be a guard that only reads as one, and an allowance of zero
    cannot sit above reality because the upper bound is equality. What is left is the stronger
    check — this reds the moment any ADR lands unfiled, which is what the arrears were.
    """
    arrears = sorted(
        adr
        for adr in _adr_ids()
        if adr < _TOPIC_CURSOR and adr not in _topic_cited_ids() and adr not in _NOT_A_TOPIC
    )
    assert len(arrears) <= _UNFILED_ARREARS_ALLOWANCE, (
        f"{len(arrears)} ADRs older than {_TOPIC_CURSOR} are cited by no row of the 'By topic' "
        f"table, against an allowance of {_UNFILED_ARREARS_ALLOWANCE}: {arrears}. Put each one in "
        "the row for its subject (updating what that row says to read now), or declare it in "
        "_NOT_A_TOPIC with the one line saying why it is not a subject."
    )


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
    # Split in two, because it was one test standing for two different facts and only one of them
    # was the fact its ADR argued. `D-2026-09-09-a-migration-run-reports-what-it-applied-not-that-
    # the-schema-matches` justifies the positive-evidence trade entirely on **privilege** — a split
    # session store whose role may use the store and may not select the ledger — and the code
    # implemented it by catching `UndefinedTable` beside `InsufficientPrivilege`, so the admitted
    # shape was much wider than the argued case. `schema_migrations` is created by the first
    # migration, so its absence is not "the check could not run"; it is the strongest possible
    # evidence that the check would fail. Driven 2026-09-19 against an empty database under the
    # chart's shipped `CHEMCLAW_SESSION_STORE=postgres`: `/readyz` answered `200 ready`, so the pod
    # joined the Route and failed every session write. The ADR's own drive used a *missing table* to
    # prove the privilege branch, which is why the branch it argues had no test at all until now.
    "test_a_ledger_it_cannot_read_does_not_take_the_pod_out_of_the_route": (
        "split into `test_a_database_with_no_migration_ledger_takes_the_pod_out_of_the_route` and "
        "`test_a_ledger_this_role_may_not_select_does_not_take_the_pod_out_of_the_route` "
        "(tests/test_service.py). The second keeps the trade this ADR argues and proves it through "
        "a real `InsufficientPrivilege` for the first time; the first reverses the half the ADR "
        "never argued, because no ledger at all is a mismatch rather than an unreadable one"
    ),
    # Retired because it asserted the opposite of what it read. It parsed the AST for an absent
    # `checkpointer=` keyword and concluded the helper graph held no checkpointer — and that absence
    # is precisely how a LangGraph subgraph asks to *inherit* its parent's, so every helper was
    # checkpointing onto its caller's saver while this stayed green.
    # `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer` measured it at 98% of what a
    # spawn cost, and the replacement observes the rows instead of re-deriving the fact.
    "test_the_helper_graph_is_compiled_without_a_checkpointer": (
        "replaced by `test_a_helper_writes_no_checkpoint_of_its_own` (tests/test_subagents.py), "
        "which drives a real spawn against a real saver and reads the namespaces off `checkpoints` "
        "rather than reading a keyword's absence off the source"
    ),
    # Deleted with the destination it charged. `D-2026-09-12-an-ambient-proxy-is-a-destination-
    # nobody-declared` moved the JWKS fetch onto httpx with `trust_env=False`, so the row it drove
    # had to leave `_env_reading_destinations` — and with it the only destination an
    # `entra_required` + `otel_enabled=false` process charged at all.
    # `D-2026-09-16-a-refusal-that-charges-a-destination-cannot-charge-an-inherited-environment`
    # restores the refusal for a different and true reason, so the replacement asserts the same
    # outcome for the same input while naming a carrier rather than that destination.
    "test_the_jwks_fetch_is_charged_by_its_own_scheme": (
        "replaced by `test_the_enforced_posture_is_refused_behind_an_undeclared_proxy` "
        "(tests/test_netguard.py), which refuses the same configuration over the inherited "
        "environment rather than over a destination that is now immune"
    ),
    # Renamed because its name asserted a *default* and the default changed. The flagged-answer ADR
    # cites it while the revision loop shipped at 0 rounds;
    # `D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` turned it on, so a test called
    # "off by default" would name a configuration nobody runs. What it pinned — that the off path is
    # a *complete* no-op, no extra model call and no record — is worth keeping, and is now driven
    # under a monkeypatch rather than off the default.
    "test_the_loop_is_off_by_default": (
        "replaced by `test_the_loop_turned_off_is_a_complete_no_op` "
        "(tests/test_answer_revision.py), which pins the same no-op against an explicitly "
        "disabled loop rather than against a default"
    ),
    # Renamed because the name asserted the opposite of what the test pinned. It said "once" and
    # drove a single call, over a branch that returned `True` unconditionally — so what it actually
    # held was "every time, forever", which is the DARK-7 failure it was written to prevent
    # (`D-2026-09-14-an-undated-note-is-not-news-every-hour`, measured at 32 of 39 shipped notes).
    "test_a_note_with_no_date_is_reported_once_rather_than_never": (
        "replaced by `test_an_undated_note_is_told_once_and_then_not_again` "
        "(tests/test_digest.py), which drives both calls and so can tell the two apart"
    ),
    # Deleted by the implementation it existed to demand. `D-2026-08-27` wrote it to fail whoever
    # re-added a retraction's storage half without the readers that honour it, and said so in its
    # own docstring; `D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports` brought the readers, so
    # what is left is an absence test asserting the opposite of what ships.
    "test_no_retraction_tier_claims_to_exist_without_the_readers_that_honour_it": (
        "replaced by `test_a_withdrawn_entry_leaves_the_evidence_set_on_every_reader` "
        "(tests/test_eln.py), which drives all five readers it demanded"
    ),
    # Deleted with the behaviour it asserted. `D-2026-09-13-an-answer-is-archived-so-the-question-
    # can-be-asked-again` archives the previous cycle's answer and allows the reopen, so the refusal
    # this test drove has no reachable input — and the raise behind it is gone with it.
    "test_a_wait_refused_by_the_projection_fails_instead_of_waiting_blind": (
        "replaced by `test_a_re_ask_of_an_answered_question_opens_through_the_activity` "
        "(tests/test_awaiting.py), which asserts the opposite outcome for the same input"
    ),
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


def test_no_retired_test_citation_is_unspent() -> None:
    """A row here is a permission *granted to a citation*, so a citation has to be asking for it.

    `test_every_test_an_adr_names_still_exists` consults this register only for names it found in
    `docs/decisions/`, so a row naming a test no ADR cites is never read — it is an exemption
    nobody spent, which is exactly the shape `test_no_exemption_outlives_its_migration` exists to
    refuse one register over. Measured by mutation: adding
    `"test_zzz_nothing_ever_cited_this"` to the register left this file green at 16 passed, and
    this register was the only one in this review missing its unspent-entry half — `KNOWN_OVERSIZED`
    has one, `NOT_YET_MEASURED` has one, `_REVIEWED_ROLLBACK_BREAKS` has one.

    The consequence of leaving it out is not cosmetic: a row is also a *silencer*, and one that
    outlives the citation it was granted for goes on silencing the name if a later test claims it
    — which the resurrection check below catches only while the name stays exactly as spelled.
    """
    cited = {
        name for path in _adr_files() for name in _TEST_CITATION.findall(path.read_text("utf-8"))
    }
    unspent = sorted(set(_RETIRED_TEST_CITATIONS) - cited)
    assert not unspent, (
        f"declared retired but cited by no ADR: {unspent}. The row exempts a citation that does "
        "not exist, so nothing ever reads it — delete it. (If you added the row in the same "
        "commit as the ADR that cites the name, check the citation's spelling against "
        "`_TEST_CITATION`: a citation the regex cannot see is the other way to reach this.)"
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


#: A commit citation as this repository writes one: `at <sha>`, `commit <sha>`, `since <sha>`.
#: Scoped to that phrasing rather than to "a hex token in backticks", which was measured first and
#: matched a SMILES string (`c1ccccc`, benzene) and four content fingerprints — a check that cries
#: wolf about prose is one nobody keeps.
_COMMIT_CITATION = re.compile(
    r"(?i)\b(?:at|commit|commits|sha|revision|since)\s+`([0-9a-f]{7,12})`"
)


def test_no_adr_cites_a_commit_a_squash_will_strand() -> None:
    """A branch SHA is unreachable from `main` the moment the branch is squash-merged.

    This repository merges branches by squash, so every commit an in-flight ADR cites is rewritten
    into one new commit with a new hash and the cited objects are reachable from nothing. The ADR
    then tells a reader to run a `git show` that fails — which is the worse half: the citation
    reads as checkable provenance right up until somebody checks it. It is not hypothetical in this
    family; the sibling repository had to correct four such citations after a squash.

    So the rule is *reachability*, asked with `git merge-base --is-ancestor` rather than
    `git cat-file -e`: the object resolves perfectly well in the authoring checkout, where the
    branch is still checked out, and that is exactly the checkout the ADR is written in. Name the
    state by what it is, or by the PR, and make the reproduction runnable from content.

    Skipped rather than passed where the answer cannot be had — no git, no `origin/main`, or **an
    object this checkout does not have** — because a check that quietly shrinks is worse than one
    that says what it did not look at.

    That third condition was missing and the omission inverted the check. On a **shallow** clone —
    which is what the container this repository provisions hands a session — `origin/main` resolves
    and the history behind it does not, so `merge-base --is-ancestor` exits non-zero for want of the
    *object* rather than for want of reachability. Measured on such a checkout: seven citations
    across five ADRs nobody had touched were reported as stranded, and all seven are reachable from
    `main` once the clone is completed. So a red gate blamed five innocent ADRs, in a message whose
    wording ("not reachable from `origin/main`") a reader has no way to tell from the real fault.
    An absent object is only a strand where the history is actually present, so the two cases are
    now distinguished: with full history this behaves exactly as before (CI's suite job clones at
    `fetch-depth: 0`), and on a shallow one it says how much it could not ask about.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:  # pragma: no cover - toolchain-dependent
        pytest.skip("no git: reachability cannot be asked")
    root = Path(__file__).resolve().parents[1]
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "origin/main"], cwd=root, capture_output=True
    ).returncode:  # pragma: no cover - a checkout with no remote
        pytest.skip("no origin/main in this checkout: reachability cannot be asked")

    shallow = (
        subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=root,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "true"
    )

    stranded: list[str] = []
    unasked: list[str] = []
    for path in _adr_files():
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            for sha in _COMMIT_CITATION.findall(line):
                citation = f"{path.name}:{number} cites {sha}"
                present = not subprocess.run(
                    ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=root, capture_output=True
                ).returncode
                if not present and shallow:
                    # The object is outside this clone's slice of history, so reachability is not a
                    # question this checkout can answer either way.
                    unasked.append(citation)
                    continue
                reachable = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", sha, "origin/main"],
                    cwd=root,
                    capture_output=True,
                )
                if reachable.returncode:
                    stranded.append(citation)
    assert not stranded, (
        "these ADRs cite commits that are not reachable from `origin/main`, so a reader on `main` "
        f"cannot resolve them: {stranded}. Cite the pull request and name the state by what it is "
        "— a squash merge rewrites every branch commit into one new hash"
    )
    if unasked:  # pragma: no cover - only on a shallow clone, and CI's suite job is not one
        pytest.skip(
            f"shallow clone: {len(unasked)} of the cited commits are outside this checkout's "
            "history, so they were not asked about (the rest passed). Run "
            "`git fetch --unshallow origin` to check them all."
        )
