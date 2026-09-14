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
    `D-2026-09-14-tools-were-never-the-variable` withdrew. The second assertion is what stops the
    correction being edited out while the flattering half of it stays.
    """
    text = _record_text()
    assert "62/100" in text and "74/100" in text, (
        "the readiness record no longer states the ChemBench result. It is the only external "
        "number this repository has, and it is 12 points worse with tools than without — which is "
        "exactly why it is the one a later edit would drop."
    )
    assert "62 → 58" in text and "D-2026-09-14-tools-were-never-the-variable" in text, (
        "the readiness record states the ChemBench pair without saying which variable moved. "
        "The arms differ by the whole system prompt as well as by the tools; with the prompt held "
        "fixed, removing every tool moves 62 → 58."
    )
