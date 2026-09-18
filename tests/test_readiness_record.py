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


#: A claim in the record about what a *setting* ships as, in the shapes the record writes it:
#: `` `name` ships at **N** ``, `` `name` **ships on** ``, and `` `retention_*_days` defaults to
#: 0 `` — a glob standing for a family whose members must agree. How many rows use each shape is
#: the record's business and is deliberately not stated here: this comment said "three shapes,
#: because three rows make that claim" over a record carrying two, which is the defect the guard
#: below exists to catch, one document up. `_SETTING_SUBJECT` is what makes the population of rows
#: derived instead.
_SHIPPED_DEFAULT = re.compile(
    r"`([a-z][a-z0-9_]*(?:\*[a-z0-9_]*)?)`\s*"
    r"(?:\*\*)?(?:ships at|defaults to)(?:\*\*)?\s*"
    r"(?:\*\*)?(\d[\d_,]*)(?:\*\*)?"
    r"|`([a-z][a-z0-9_]*)`\s*\*\*ships on\*\*"
)

#: Every backticked token in the record that *could* name a setting. The claims above are checked
#: to cover this population, so a subject whose claim is reworded out of `_SHIPPED_DEFAULT` is
#: still here and still owed one — which is what makes a reword fail instead of pass.
_SETTING_SUBJECT = re.compile(r"`([a-z][a-z0-9_]*(?:\*[a-z0-9_]*)?)`")

#: Settings the record names without claiming what they ship as, each argued. `entra_required` is
#: named as a *condition* ("under `entra_required`, the production app runs against a real HTTP
#: JWKS", "an exposed bind without `entra_required`") rather than as a value, so there is no
#: default for the config to disagree with. A new entry here is a deliberate exemption, not a
#: default: a row that names a setting and says nothing about what it ships is a row a deployment
#: team cannot act on.
_NAMED_WITHOUT_A_SHIPPED_DEFAULT = {"entra_required"}


def test_a_claim_about_a_shipped_default_agrees_with_the_setting() -> None:
    """Two accepted risks went stale in two days and nothing here noticed.

    `agent_max_turn_billed_tokens` shipped at 0, and §4 said so. Another session turned it on
    (`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has`, default 300,000) and this
    record went on telling a deployment team that a turn's spend was unbounded — the *reassuring*
    direction of staleness inverted: a record understating what it has is read as honest right up
    until somebody re-derives it. Nothing failed, because every test this file already runs asks
    whether a named control *exists*, and this control existed the whole time. What changed was its
    default, which no assertion here could see.

    So a row that states what a setting ships as is checked against the setting, in the shapes the
    record writes: "ships at N", "ships on" (non-zero, deliberately not a number — the record's own
    preamble refuses figures that go stale on a commit), and a `*` glob standing for a family every
    member of which must agree, which is how the retention row states a posture over five settings
    at once.

    **The subjects are derived, not counted, and the first version of this counted.** It anchored
    on `assert claims` — one parsed claim was enough — and only two parse, so rewording either left
    the other satisfying the anchor. Driven: reword the spend row's `agent_max_turn_billed_tokens`
    **ships on** to "is **on by default**", set the field back to `Field(default=0)`, and this was
    `5 passed` while the record told a deployment team a spend ceiling was on that ships off — the
    exact defect it was written for, in the reassuring direction, one layer up. So every backticked
    token in the record that resolves to a real settings field must be claimed by one of those
    shapes or be on `_NAMED_WITHOUT_A_SHIPPED_DEFAULT` with its reason. A reword drops the subject
    out of the claimed set and leaves it in the named set, which fails.

    It cannot check the sentence around the claim. A row may say "which is off" beside a setting
    that is on and this will not see it; what it sees is the number, which is the half that moved
    both times.
    """
    from chemclaw.core.config import settings

    fields = type(settings).model_fields
    record = _record_text()
    claims = list(_SHIPPED_DEFAULT.finditer(record))

    def _fields_named(token: str) -> list[str]:
        """The settings a backticked token names — one, or a family when it carries a `*`."""
        return [field for field in fields if re.fullmatch(token.replace("*", ".*"), field)]

    named = {token for token in _SETTING_SUBJECT.findall(record) if _fields_named(token)}
    claimed = {match.group(1) or match.group(3) for match in claims}
    assert named, (
        "the record names no setting at all any more; the rows stating what this deployment ships "
        "have gone, or they have stopped writing a setting's name in backticks, and the claims "
        "below are then checked against nothing."
    )
    unclaimed = named - claimed - _NAMED_WITHOUT_A_SHIPPED_DEFAULT
    assert not unclaimed, (
        f"the record names {sorted(unclaimed)} without saying what it ships as in a shape this "
        "reads. A row reworded out of that shape is how the spend row went stale in the "
        "reassuring direction: say it as `name` ships at **N** or `name` **ships on**, or add the "
        "setting to _NAMED_WITHOUT_A_SHIPPED_DEFAULT with the reason it makes no such claim."
    )

    wrong = []
    for match in claims:
        name, stated, on = match.group(1), match.group(2), match.group(3)
        if on is not None:
            value = getattr(settings, on)
            if not value:
                wrong.append(f"`{on}` is claimed to ship on and ships {value!r}")
            continue
        assert name is not None
        matched = _fields_named(name)
        assert matched, (
            f"the record names `{name}`, which is no setting on this config. This used to read "
            "`matched = [name]` for a non-glob name, so it could not fire for the shape the record "
            "actually writes and an unknown name raised a bare AttributeError from `getattr` "
            "below instead."
        )
        expected = int(stated.replace("_", "").replace(",", ""))
        for field in matched:
            value = getattr(settings, field)
            if value != expected:
                wrong.append(f"`{field}` is claimed to ship at {expected} and ships {value!r}")

    assert not wrong, (
        f"the readiness record states a shipped default the config disagrees with: {wrong}. A row "
        "that is stale in the reassuring direction — a gap the record still accepts and the code "
        "has closed — reads as honest until somebody re-derives it."
    )
