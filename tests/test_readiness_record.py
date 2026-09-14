"""The readiness record may only name controls that exist.

`docs/decisions/D-2026-09-14-what-a-deployment-team-is-getting.md` states, in four sections, what
this repository enforces, bounds, measures and explicitly accepts. Its own rule is that **every
clause names the test that holds it** — a clause with no test is rewritten as an accepted risk or
deleted.

That rule is exactly the kind a document can state and then stop obeying. This repository has the
receipts: `mcp_servers/calc/` was asserted deleted across four ADRs while still dispatchable,
`audit_events.agent` was documented as naming the agent beside the human on every row it had never
carried, and `docs/guides/runbook.md` described a `trivy` gate that ran nowhere. A readiness record
is the document most likely to be read instead of the code, so a citation in it that resolves to
nothing is worse than an omission: it is a control a deployment team believes they have.

**What this asserts and what it cannot.** It resolves every `tests/…` path and every
`file::test_name` the record names against what is actually on disk and, for the named functions,
against the module's own source. What no test can check is whether the clause beside a citation is
a *fair description* of what that test proves — that is a review matter, like an ADR's prose, and
saying so is part of the record's own preamble.

Deliberately not a count. The record grows and shrinks; what must hold is that nothing in it points
at a control that is not there.
"""

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_RECORD = REPO_ROOT / "docs" / "decisions" / "D-2026-09-14-what-a-deployment-team-is-getting.md"

# A backticked path under `tests/`, optionally with a `::` and one function name — which is how the
# record writes a citation. The function half is optional because most clauses name a module and a
# few name one assertion inside it.
_CITATION = re.compile(r"`(tests/[A-Za-z0-9_/]+\.py)(?:::([A-Za-z0-9_]+))?`")


def _record_text() -> str:
    """The record, or a failure naming it — a missing record is the loudest possible drift."""
    assert _RECORD.exists(), (
        f"{_RECORD.relative_to(REPO_ROOT)} is gone. The readiness record is a merged ADR; a "
        "decision that has changed gets a new ADR that supersedes it, never a deletion."
    )
    return _RECORD.read_text(encoding="utf-8")


def _defined_functions(module: Path) -> set[str]:
    """Every top-level function name a test module defines.

    Parsed rather than imported: importing every module the record cites would pull in the whole
    suite's fixtures for an assertion about names, and a module that fails to import for an
    unrelated reason would report as a missing citation.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_every_test_the_readiness_record_names_exists() -> None:
    """A citation that resolves to nothing is a control a deployment team believes they have."""
    citations = _CITATION.findall(_record_text())
    assert len(citations) > 20, (
        f"only {len(citations)} test citation(s) parsed out of the readiness record; either the "
        "record has been gutted or this pattern no longer matches how it writes them"
    )

    missing: list[str] = []
    for relative, function in citations:
        module = REPO_ROOT / relative
        if not module.exists():
            missing.append(relative)
            continue
        if function and function not in _defined_functions(module):
            missing.append(f"{relative}::{function}")

    assert not missing, (
        f"the readiness record names {len(missing)} test(s) that do not exist: "
        f"{sorted(set(missing))}. "
        "Either the control moved and the record has to follow it, or the clause was written "
        "against a test nobody wrote — in which case it is an accepted risk, not a control."
    )


def test_the_record_still_carries_all_four_sections() -> None:
    """Three sections of good news and one of accepted risk is the shape; three alone is a lie.

    The fourth section is the one that decides whether this system should be deployed, and it is
    the one a later edit is most tempted to trim. Asserted by heading, because a record that keeps
    "enforced / bounded / measured" and loses "accepted" reads as a stronger claim than the
    original while being strictly less honest.
    """
    text = _record_text()
    for heading in (
        "## 1. Enforced",
        "## 2. Bounded",
        "## 3. Measured",
        "## 4. Accepted",
    ):
        assert heading in text, f"the readiness record no longer carries `{heading}`"


def test_the_external_benchmark_number_is_still_in_it() -> None:
    """The one figure nobody here chose the questions for, and the one that flatters least.

    62/100 with every tool bound against 74/100 under the toolless control arm. A readiness record
    that quietly loses it is the failure this whole programme has been correcting — so the number
    is asserted here rather than trusted to survive an edit.

    **And the variable is asserted beside it**, because the pair was published as a tools contrast
    and is not one: the control arm replaces the whole system prompt, so a record stating the two
    numbers without naming what moved between them repeats the attribution
    `D-2026-09-14-tools-were-never-the-variable` withdrew. That assertion is what stops the
    correction being edited out while the flattering half of it stays.

    **Asserted as the claim rather than as the digits, which is a change**
    (`D-2026-09-14-a-pinned-figure-is-a-control-only-if-it-can-go-stale`). This test pinned the
    literal `62 → 58`, a figure from a three-arm run whose transcripts are in no tree here and
    which nothing in this repository can re-derive. A pin like that cannot notice the number going
    stale — only a document that stops repeating it — so it does not detect staleness, it enforces
    it, and whoever re-runs the arm on a gateway with a balance would have had to edit the control
    to record the measurement. `62/100` and `74/100` stay pinned because `make live-benchmark`
    produces them from a keyed corpus this repository vendors.
    """
    text = _record_text()
    assert "62/100" in text and "74/100" in text, (
        "the readiness record no longer states the ChemBench result. It is the only external "
        "number this repository has, and it is 12 points worse with tools than without — which is "
        "exactly why it is the one a later edit would drop."
    )
    row = next((line for line in text.splitlines() if "62/100" in line), "")
    assert "D-2026-09-14-tools-were-never-the-variable" in row, (
        "the readiness record states the ChemBench pair without citing the ADR that withdrew its "
        "attribution. The arms differ by the whole system prompt as well as by the tools."
    )
    assert "system prompt" in row, (
        "the ChemBench row no longer names the variable that moved between the two arms, which is "
        "the correction rather than the measurement."
    )
    assert "not reproducible from this tree" in row, (
        "the ChemBench row carries the three-arm figures without saying that run's record is not "
        "in this tree. Section 3's heading promises a number somebody can reproduce, and the row "
        "below it ships 331 transcripts for exactly that reason."
    )


#: Where the live run the record cites left its per-probe transcripts. The record names this
#: directory, so the two figures in that row are the one pair in §3 a reader can re-derive here.
_CORPUS_TRANSCRIPTS = REPO_ROOT / "tasks" / "live-test" / "transcripts" / "corpus"


def test_the_live_run_row_counts_what_its_transcripts_hold() -> None:
    """The one row in §3 whose figures this tree can re-derive, derived rather than believed.

    It shipped saying **27** distinct tools where the cited transcripts hold **26** across
    `tools_called ∪ tools_failed ∪ tool_results` — a figure nothing could check, in the row that
    exists precisely because its evidence is committed. Section 3's other rows either name a target
    that reproduces them or now say their run's record is elsewhere
    (`D-2026-09-14-a-pinned-figure-is-a-control-only-if-it-can-go-stale`); this one's evidence is
    right here, so the digits are held against it.

    **A second committed run fails this rather than being averaged in.** The row describes one
    execution on one date. If another lands, that row has to be rewritten, and a check that quietly
    unioned two runs would let it go on describing the first.
    """
    runs = sorted(path for path in _CORPUS_TRANSCRIPTS.iterdir() if path.is_dir())
    assert len(runs) == 1, (
        f"{len(runs)} live-probe runs are committed under {_CORPUS_TRANSCRIPTS}. The readiness "
        "record's live-run row describes one execution on one date; rewrite it for the run it "
        "should now cite rather than leaving it pointing at a directory holding several."
    )
    transcripts = sorted(runs[0].glob("*.json"))
    tools: set[str] = set()
    for path in transcripts:
        outcome = json.loads(path.read_text(encoding="utf-8"))["outcome"]
        for field in ("tools_called", "tools_failed"):
            tools |= {str(name) for name in outcome.get(field) or []}
        for result in outcome.get("tool_results") or []:
            name = result.get("name") if isinstance(result, dict) else result
            if name:
                tools.add(str(name))

    row = next(line for line in _record_text().splitlines() if "distinct tools exercised" in line)
    assert f"then {len(transcripts)} probes" in row, (
        f"the live-run row does not say {len(transcripts)} probes, which is how many transcripts "
        f"{runs[0].name} holds."
    )
    assert f"{len(tools)} distinct tools exercised" in row, (
        f"the live-run row's tool count disagrees with its own transcripts, which name "
        f"{len(tools)}: {sorted(tools)}"
    )
