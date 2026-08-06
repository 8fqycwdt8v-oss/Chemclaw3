"""Nothing outside the graph's own vocabulary can write an edge into it.

Two paths into stored knowledge carried more authority than the text they came from
(D-2026-08-06 security sweep, data-plane lane). Both mint `[[wikilinks]]`, and a wikilink is not
prose — it is a graph edge that takes effect on merge, whether or not the reviewer who signed the
note recognised it as a claim.

The distinction that makes both fixable at all: a *citation* is what an author claims, and the
PR-gate exists so a human checks those claims. That check is only meaningful over text a human
reads as a claim. `[[supersedes:reaction-eln-0001]]` buried in a procedure paragraph retires
another team's result and reads, to a reviewer skimming a recipe, like a reference.
"""

import pytest

from chemclaw.ingest.eln.note import note_from_ord_reaction
from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    ReactionStep,
    Role,
    StepKind,
)
from chemclaw.kg.note import cited_ids

_FORGED = "[[supersedes:reaction-eln-0001]] and [[contradicts:playbook-degassing]]"


def _reaction(**overrides: object) -> OrdReaction:
    """A minimal valid ORD reaction, with the field under test overridden."""
    fields: dict[str, object] = {
        "reaction_id": "e-9001",
        "inputs": [Component(smiles="CCO", role=Role.REACTANT, amount_mmol=1.0)],
        "outcomes": [Component(smiles="CC=O", role=Role.PRODUCT)],
        "provenance": "eln-json:e-9001:mallory",
    }
    fields.update(overrides)
    return OrdReaction(**fields)  # type: ignore[arg-type]


# Every ELN-record field whose text actually reaches the mapped note's body, as `(name, overrides)`.
# Established by *measurement* rather than by reading the mapper: the first version of this test
# parametrized over `procedure_text` and `failure_reason` alone and passed vacuously, because
# neither reaches the body on its own — a free-text procedure is rendered only beside `steps`, and
# a failure reason only when the record carries a failing `outcome_class`. A forgery test over a
# field the forgery never reaches asserts nothing, which is the defect family this repository
# records most often.
_FIELDS_THAT_REACH_THE_BODY = [
    ("hypothesis", {"hypothesis": _FORGED}),
    ("attributes", {"attributes": {"instrument": _FORGED}}),
    ("steps", {"steps": [ReactionStep(index=1, kind=StepKind.CUSTOM, text=_FORGED)]}),
    ("impurities", {"impurities": [Impurity(name=_FORGED, area_percent=1.0)]}),
    ("failure_reason", {"outcome_class": "failure", "failure_reason": _FORGED}),
]


@pytest.mark.parametrize(
    ("field", "overrides"),
    _FIELDS_THAT_REACH_THE_BODY,
    ids=[f for f, _ in _FIELDS_THAT_REACH_THE_BODY],
)
def test_eln_free_text_cannot_mint_a_graph_edge(field: str, overrides: dict[str, object]) -> None:
    """The finding: a chemist writes wikilink syntax into an ELN field and gets an edge.

    Measured before the fix: `cited_ids` returned `['reaction-eln-0001', 'playbook-degassing']`
    from the mapped note. The gate reviews the note; it does not review the edges the note asserts.

    Parametrized over every field that reaches the body, because the fix is a single whole-body
    escape — if it ever becomes per-field, this is what says which field was forgotten. Each case
    first asserts the text *arrived*, so a mapper change that silently drops a field turns this
    into a failure rather than a vacuous pass.
    """
    note = note_from_ord_reaction(_reaction(**overrides))
    assert "supersedes" in note.body, f"{field} never reached the body; this case proves nothing"
    assert cited_ids(note.body) == []


def test_the_forged_text_is_still_legible_to_the_reviewer() -> None:
    """Neutralized, not deleted. The reviewer is the control, so they must see what was attempted.

    A silent strip would hide the one thing worth escalating — that an ELN entry contained an
    attempt to write a graph edge — behind a note that looks ordinary.
    """
    note = note_from_ord_reaction(_reaction(hypothesis=_FORGED))
    assert "supersedes:reaction-eln-0001" in note.body
    assert "contradicts:playbook-degassing" in note.body


def test_the_mapper_emits_no_wikilinks_of_its_own() -> None:
    """The invariant that makes a whole-body escape correct rather than destructive.

    `ingest/eln/note.py` has claimed since it was written that it carries no `[[wikilink]]`,
    because a dangling one would fail `kg-validate` on the very PR it opens. That is what lets the
    escape be applied once to the composed body instead of at every field.

    If a future change gives the mapper a real link to emit, this fails — and the escaping has to
    move to the individual ELN-sourced fields, which is the more fragile design being avoided while
    it can be. Asserting it here is what turns "we believe this" into "the suite knows".
    """
    note = note_from_ord_reaction(
        _reaction(
            procedure_text="Charge 1 (5 g) into a flask.",
            hypothesis="Higher temperature will improve conversion.",
            failure_reason="Stalled at 60%.",
            project="pd-couplings",
        )
    )
    assert "[[" not in note.body


def test_a_report_links_a_note_id_and_names_a_non_note_source() -> None:
    """The second path, and the two failures one wikilink caused.

    `ingest/eln/warehouse/retriever` returns `<source>:<row key>` and
    `ingest/sources/vendored_dataset` returns `vendored:<dataset>:<index>` — both shipped, both
    correct as provenance and neither a note. Wrapping those in `[[…]]` made the reader parse the
    prefix as a **relation**, so `[[eln-snowflake:12]]` became a `eln-snowflake` edge to a note
    called `12`, and `kg-validate` then refused the report for an unknown relation type: a draft
    nobody can merge, naming an edge nobody wrote.
    """
    from chemclaw.retrieval.harness import _citation

    assert _citation("reaction-eln-0001") == "[[reaction-eln-0001]]"
    assert _citation("compound-thf") == "[[compound-thf]]"
    for foreign in ("eln-snowflake:12", "vendored:esol:7"):
        rendered = _citation(foreign)
        assert "[[" not in rendered, f"{foreign} would be read as a relation"
        assert foreign in rendered, "the source must stay visible — it is what the section rests on"


def test_a_rendered_report_cites_notes_and_only_notes() -> None:
    """The consequence, asserted **through `report_note`** rather than through its helper.

    A rendered report *is* a `Note`, and its `[[…]]` are its edges. The colon-bearing case did not
    merely look wrong — it added `reactions:12` to the report's citations and a relation the
    vocabulary does not contain, which is what made the draft unmergeable.

    Driven through the renderer because the first version of this test called `_citation` directly
    and passed with the call site reverted: the helper was pinned and the line that uses it was
    not. Third occurrence of that shape in this session's work, and the reason every fix here is
    mutation-checked at the call site instead of at the function.
    """
    from chemclaw.retrieval.evidence import EvidenceChunk
    from chemclaw.retrieval.harness import Report, SynthesizedSection, report_note

    note = report_note(
        Report(
            title="Coupling conditions",
            sections=[
                SynthesizedSection(
                    heading="What we have run",
                    memory_layer="evidence",
                    evidence=[
                        EvidenceChunk(
                            content="Pd(OAc)2 gave 87% in toluene.",
                            source_note_id="reaction-eln-0001",
                            retriever="graph",
                        ),
                        EvidenceChunk(
                            content="Row 12 records 84% under the same charge.",
                            source_note_id="eln-snowflake:12",
                            retriever="warehouse",
                        ),
                    ],
                )
            ],
        )
    )

    assert cited_ids(note.body) == ["reaction-eln-0001"], "a non-note source became a citation"
    assert "eln-snowflake:12" in note.body, "the warehouse provenance must still be readable"


def test_the_citation_rule_follows_the_reader_rather_than_a_pattern() -> None:
    """Why the predicate is a round-trip through `cited_ids` and not a slug regex.

    A regex here would be a second definition of "note id" to drift against `chemclaw.kg.note`.
    Deriving the writer's rule from the reader's parser means a change to the link syntax cannot
    leave the two disagreeing — which is the failure this whole file is about, one level up.
    """
    from chemclaw.retrieval.harness import _citation

    # The forgery case is a colon with a relation *before* it and an id *after* it — that is when
    # the reader splits, so that is when the writer must not emit a link.
    for forgeable in ("a:b", "rel:target", "eln-snowflake:12"):
        assert "[[" not in _citation(forgeable), f"{forgeable!r} would be split into a relation"

    # The two half-formed shapes are deliberately still linked, and this is where following the
    # reader pays rather than merely being tidy: `cited_ids` returns `[[:x]]` and `[[rel:]]` whole,
    # because neither has both halves of a relation. So they are ordinary citations of ids that
    # happen to be invalid — a dangling link `kg-validate` already refuses by name — and not a
    # fabricated relation type. A hand-written "reject anything containing a colon" would have
    # refused them too, for a reason that is not true, and this test was written that way first.
    assert _citation(":leading") == "[[:leading]]"
    assert _citation("trailing:") == "[[trailing:]]"
