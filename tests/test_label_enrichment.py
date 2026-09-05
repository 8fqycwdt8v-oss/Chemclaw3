"""The enrichment drain: what it derives, what it keeps, and what it must never stall on.

The property under test is the one the whole background service rests on — *`provides` is never a
skip*. A source that ships NameRxn names for two thirds of its corpus is exactly as much work for
this drain as one that ships none, because the policy describes the source's intent and the row
describes the row. Everything else here is about not wedging: a reaction the server chokes on must
cost that reaction and not the corpus behind it.

The labelling server itself is faked. That is not a shortcut around an integration test — the real
one lives in a separate repository and image — it is the same seam `tests/calc_server_fake.py`
uses for the calculation server, and it is what lets the merge rule be asserted against answers
chosen to exercise it.
"""

import asyncio
from datetime import date

import pytest

from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.labels.enrich import label_stale
from chemclaw.ingest.labels.labeller import (
    LabelServerError,
    LabelToolError,
    ReactionNaming,
    ReactionRepresentation,
    RxnLabelServer,
)
from chemclaw.ingest.labels.merge import merge
from chemclaw.science.labels.policy import LabelPolicy
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.store import InMemoryLabelIndex
from chemclaw.science.labels.vocabulary import LabelGroup, SpeciesRole

_VERSION = "rxnlabel@1:std5:roles1"

# A Buchwald-Hartwig, because it is the reaction three of the six precedent questions name and the
# only one where every role in the vocabulary is actually distinguishable.
_RECORD = "Brc1ccccc1.NC1CCCCC1>CC(C)(C)P(C(C)(C)C)C(C)(C)C.CC#N>c1ccc(NC2CCCCC2)cc1"


def _row(reaction_id: str = "r1", source: str = "pistachio", **overrides: object) -> ReactionLabel:
    """A record-phase row with four species, as a corpus drain would have written it."""
    base = ReactionLabel(
        source=source,
        reaction_id=reaction_id,
        record_smiles=_RECORD,
        citation="US9376441B2",
        performed_on=date(2016, 6, 28),
        species=[
            SpeciesLabel(ordinal=0, smiles="Brc1ccccc1", role="reactant"),
            SpeciesLabel(ordinal=1, smiles="NC1CCCCC1", role="reactant"),
            SpeciesLabel(ordinal=2, smiles="CC(C)(C)P(C(C)(C)C)C(C)(C)C", role="reagent"),
            SpeciesLabel(ordinal=3, smiles="c1ccc(NC2CCCCC2)cc1", role="product"),
        ],
    )
    return base.model_copy(update=overrides) if overrides else base


def _representation(reaction_id: str = "r1") -> ReactionRepresentation:
    """What the labeller answers: an atom map and one entry per species, in the order sent."""
    return ReactionRepresentation.model_validate(
        {
            "id": reaction_id,
            "mapped_smiles": "[Br:1][c:2]1ccccc1>>[c:2]1ccc(N)cc1",
            "species": [
                {"role": "starting-material", "scaffold": "c1ccccc1"},
                {"role": "starting-material", "scaffold": "C1CCCCC1"},
                {"role": "ligand"},
                {"role": "product", "functional_groups": ["secondary amine"]},
            ],
        }
    )


def _naming(reaction_id: str = "r1") -> ReactionNaming:
    """What the classifier answers for the same reaction."""
    return ReactionNaming.model_validate(
        {
            "id": reaction_id,
            "named_reaction": "Buchwald-Hartwig amination",
            "reaction_class": "Heteroatom alkylation and arylation",
            "rxno_id": "RXNO:0000192",
            "confidence": 0.94,
            "method": "smirks",
        }
    )


class _FakeLabeller:
    """A labelling server that answers from fixtures, and can be told to refuse or to fall over."""

    def __init__(
        self,
        *,
        refuse: set[str] | None = None,
        refuse_batches: bool = False,
        outage: bool = False,
    ) -> None:
        self.refuse = refuse or set()
        self.refuse_batches = refuse_batches
        self.outage = outage
        self.calls: list[int] = []
        # The ids each `represent` call was handed, so a test can assert what went on the wire.
        self.sent: list[list[str]] = []

    async def version(self) -> str:
        return _VERSION

    async def represent(
        self, reactions: list[tuple[str, str, list[str]]]
    ) -> dict[str, ReactionRepresentation]:
        self.calls.append(len(reactions))
        self.sent.append([rid for rid, _, _ in reactions])
        self._maybe_fail([rid for rid, _, _ in reactions])
        return {rid: _representation(rid) for rid, _, _ in reactions if not self._refused(rid)}

    async def name(self, reactions: list[tuple[str, str]]) -> dict[str, ReactionNaming]:
        self._maybe_fail([rid for rid, _ in reactions])
        return {rid: _naming(rid) for rid, _ in reactions if not self._refused(rid)}

    def _refused(self, wire_id: str) -> bool:
        """Whether this call is for a reaction the test told the server to refuse.

        The drain sends a *correlation token*, not the reaction id — it has to, because the index
        keys on `(source, reaction_id)` and one batch can hold two sources using one id. The token
        still names the reaction it stands for, which is what lets a test go on saying "refuse r2".
        """
        return wire_id.split(":", 1)[-1] in self.refuse

    def _maybe_fail(self, ids: list[str]) -> None:
        if self.outage:
            raise LabelServerError("the labelling server is not answering")
        if self.refuse_batches and len(ids) > 1:
            raise LabelToolError("batch refused")
        if len(ids) == 1 and self._refused(ids[0]):
            raise LabelToolError(f"cannot parse {ids[0]}")


# --- the merge rule ------------------------------------------------------------------------


def test_a_group_the_source_provides_is_still_derived_where_the_row_is_empty() -> None:
    """The whole of "the database will not have all these labels in the beginning".

    Pistachio declares that it provides named reactions. It ships them for part of its corpus — the
    published figure is roughly two thirds — and the rest arrive with the column empty. Those rows
    must be labelled, or the answer to "which ligands for Buchwald couplings" is drawn from
    whichever fraction NameRxn happened to classify, silently.
    """
    policy = LabelPolicy(provides=frozenset({LabelGroup.NAMED_REACTION}))
    merged = merge(_row(), policy, _representation(), _naming())
    assert merged.named_reaction == "Buchwald-Hartwig amination"
    assert merged.method == "smirks"


def test_a_group_the_source_provides_is_kept_where_the_row_has_it() -> None:
    """And the two thirds it *did* classify are not re-derived and not overwritten."""
    policy = LabelPolicy(provides=frozenset({LabelGroup.NAMED_REACTION}))
    carried = _row(named_reaction="Bromo Suzuki coupling", rxno_id="RXNO:0000140")
    merged = merge(carried, policy, _representation(), _naming())
    assert merged.named_reaction == "Bromo Suzuki coupling"
    # And it is marked as the corpus's own claim rather than ours, because a chemist reading a
    # frequency table is entitled to know which they are looking at.
    assert merged.method == "source"


def test_override_re_derives_a_group_the_source_did_supply() -> None:
    """An ELN's roles are a typed column, not a chemistry judgment; `override` is what says so."""
    policy = LabelPolicy(
        provides=frozenset({LabelGroup.SPECIES_ROLES}),
        override=frozenset({LabelGroup.SPECIES_ROLES}),
    )
    typed = _row(
        species=[s.model_copy(update={"derived_role": SpeciesRole.REAGENT}) for s in _row().species]
    )
    merged = merge(typed, policy, _representation(), _naming())
    assert [s.derived_role for s in merged.species] == [
        SpeciesRole.STARTING_MATERIAL,
        SpeciesRole.STARTING_MATERIAL,
        SpeciesRole.LIGAND,
        SpeciesRole.PRODUCT,
    ]


def test_the_ligand_is_the_answer_the_recorded_role_could_not_give() -> None:
    """`reagent` in, `ligand` out — the one derivation the precedent questions actually need.

    The recorded vocabulary has five values and none of them is "ligand", which is why widening
    `Role` was the wrong fix and a second, derived vocabulary is the right one.
    """
    merged = merge(_row(), LabelPolicy(), _representation(), _naming())
    ligand = next(s for s in merged.species if s.smiles.startswith("CC(C)(C)P"))
    assert ligand.role == "reagent"
    assert ligand.derived_role is SpeciesRole.LIGAND


def test_an_unanswered_half_does_not_cost_the_other() -> None:
    """A reaction the atom mapper chokes on may still be named, and must be."""
    named_only = merge(_row(), LabelPolicy(), None, _naming())
    assert named_only.named_reaction == "Buchwald-Hartwig amination"
    assert named_only.mapped_smiles is None
    # With no representation, roles fall back to the coarse map of what the source recorded — a
    # floor, never a guess: it does not invent the ligand.
    assert named_only.species[2].derived_role is SpeciesRole.REAGENT

    mapped_only = merge(_row(), LabelPolicy(), _representation(), None)
    assert mapped_only.named_reaction is None
    assert mapped_only.species[2].derived_role is SpeciesRole.LIGAND


def test_a_role_this_build_does_not_know_becomes_unknown_rather_than_a_failure() -> None:
    """The labeller is versioned separately, so it may learn a role before this repository does."""
    ahead = ReactionRepresentation.model_validate(
        {"id": "r1", "species": [{"role": "phase-transfer-catalyst"}] * 4}
    )
    merged = merge(_row(), LabelPolicy(), ahead, None)
    assert all(s.derived_role is SpeciesRole.UNKNOWN for s in merged.species)


# --- the drain -----------------------------------------------------------------------------


def test_a_drain_pass_labels_and_stamps_and_reports_more() -> None:
    """One bounded pass: label what is stale, stamp it, and say whether the backlog is drained."""

    async def _run() -> None:
        index = InMemoryLabelIndex()
        for n in range(3):
            await index.record(_row(f"r{n}"))
        policies = {"pistachio": LabelPolicy(provides=frozenset({LabelGroup.NAMED_REACTION}))}

        first = await label_stale(index, _FakeLabeller(), policies, _VERSION, limit=2)
        assert (first.labelled, first.has_more) == (2, True)
        second = await label_stale(index, _FakeLabeller(), policies, _VERSION, limit=2)
        assert (second.labelled, second.has_more) == (1, False)

        assert await index.stale(_VERSION, limit=10) == []
        coverage = await index.coverage(_VERSION)
        assert (coverage.labelled, coverage.total) == (3, 3)
        assert coverage.verdict.startswith("COMPLETE")

    asyncio.run(_run())


def test_one_unlabellable_reaction_does_not_stall_the_corpus_behind_it() -> None:
    """`stale()` is deterministic, so a refusal that failed the batch would repeat forever.

    This is the failure `reembed_stale` was changed to prevent one index over, where a single
    un-embeddable chunk stopped document indexing for every share, permanently. The reaction is
    still *stamped* — it leaves the stale set carrying nothing derived, which the coverage report
    counts honestly — because the alternative is a row the drain re-reads on every pass forever.
    """

    async def _run() -> None:
        index = InMemoryLabelIndex()
        for n in range(3):
            await index.record(_row(f"r{n}"))
        labeller = _FakeLabeller(refuse={"r1"}, refuse_batches=True)

        report = await label_stale(index, labeller, {}, _VERSION, limit=10)
        assert report.labelled == 3
        assert report.unlabelled == 1
        assert await index.stale(_VERSION, limit=10) == []

        rows = {r.reaction_id: r for r in await index.stale("next-version", limit=10)}
        assert rows["r0"].named_reaction == "Buchwald-Hartwig amination"
        assert rows["r1"].named_reaction is None

    asyncio.run(_run())


def test_an_outage_propagates_instead_of_becoming_200_doomed_single_calls() -> None:
    """A server that is not there is Temporal's problem, not something to retry per reaction."""

    async def _run() -> None:
        index = InMemoryLabelIndex()
        await index.record(_row())
        with pytest.raises(LabelServerError):
            await label_stale(index, _FakeLabeller(outage=True), {}, _VERSION, limit=10)
        # Nothing was stamped, so the next pass sees the same work.
        assert len(await index.stale(_VERSION, limit=10)) == 1

    asyncio.run(_run())


def test_a_short_species_list_costs_the_roles_and_not_the_atom_map(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A species list that does not match what was sent is bad data, and nothing used to say so.

    `ReactionRepresentation.species` is documented as "one entry per species sent" and the roles
    are matched back **positionally** — `merge._species` reads `answered[index]`. That guard only
    stops the read running off the end; it cannot see that an answer for 3 of 4 species has shifted
    every role by one, so a reactant is stored as the solvent and the solvent as the catalyst. The
    server is versioned separately from this repository (`_role`'s leniency is written for exactly
    that).

    **Only the positional half is unusable, and dropping the answer threw away the other half.**
    A representation also carries `mapped_smiles`, which has nothing to do with the species
    contract — and `enrich.label_stale` stamps *every* stale row with the current
    `labeller_version` whether or not the server answered, so the reaction leaves `stale()` and no
    later pass revisits it: the atom map was lost permanently, not "this pass". Blanking `species`
    keeps the answer, and `merge._species` then takes the floor it already documents ("a short or
    absent answer falls back to `species_role_from`", the coarse map of what the source recorded).
    """

    async def _run() -> None:
        server = RxnLabelServer()
        short: dict[str, object] = {
            "results": [
                {
                    "id": "r1",
                    "mapped_smiles": "[Br:1][c:2]1ccccc1>>[c:2]1ccc(N)cc1",
                    "species": [{"role": "solvent"}, {"role": "catalyst"}, {"role": "catalyst"}],
                },
                {"id": "r2", "species": [{"role": "starting-material"}]},
                {"id": "r3", "species": []},
            ]
        }

        async def _fake_call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
            return short

        # The transport is the one thing this seam does not decide; the arity contract is.
        monkeypatch.setattr(server, "_call", _fake_call)
        answers = await server.represent(
            [
                ("r1", _RECORD, ["CCO", "O=C=O", "CCOCC", "[Pd]"]),
                ("r2", _RECORD, ["CCO"]),
                ("r3", _RECORD, ["CCO", "O=C=O"]),
            ]
        )

        assert sorted(answers) == ["r1", "r2", "r3"]
        assert answers["r1"].species == [], (
            "an answer for 3 of the 4 species sent is unusable: every role after the gap belongs "
            "to a different molecule"
        )
        assert answers["r1"].mapped_smiles == "[Br:1][c:2]1ccccc1>>[c:2]1ccc(N)cc1", (
            "the atom map is not positional and was not short; it must survive the species half"
        )
        assert "r1" in caplog.text and "3" in caplog.text and "4" in caplog.text

        # What the row is actually stamped with: the map kept, the roles from the source's own
        # coarse map rather than from three answers matched onto four species.
        merged = merge(_row(), LabelPolicy(), answers["r1"], None)
        assert merged.mapped_smiles == "[Br:1][c:2]1ccccc1>>[c:2]1ccc(N)cc1"
        assert [species.derived_role for species in merged.species] == [
            SpeciesRole.STARTING_MATERIAL,
            SpeciesRole.STARTING_MATERIAL,
            SpeciesRole.REAGENT,
            SpeciesRole.PRODUCT,
        ], "the coarse floor, not three roles shifted onto four species"

    caplog.set_level("WARNING")
    asyncio.run(_run())


def test_the_outage_error_is_not_bad_data() -> None:
    """The retry classification is the whole reason there are two error classes."""
    assert not isinstance(LabelServerError("x"), ChemclawError)
    assert isinstance(LabelToolError("x"), ChemclawError)


def test_the_drain_sends_one_batch_not_one_call_per_reaction() -> None:
    """13M reactions at a round trip each is 13M round trips; at `label_batch_size` it is 65,000."""

    async def _run() -> None:
        index = InMemoryLabelIndex()
        for n in range(5):
            await index.record(_row(f"r{n}"))
        labeller = _FakeLabeller()
        await label_stale(index, labeller, {}, _VERSION, limit=5)
        assert labeller.calls == [5]

    asyncio.run(_run())


# --- what the drain reads, and how it matches an answer back to a row -------------------------


def test_a_source_that_declares_no_labels_block_is_still_drained() -> None:
    """The requirement, as a test: every reaction corpus gets labelled, not only declaring ones.

    The drain used to narrow `stale()` to the sources that declared a `labels:` block. Exactly one
    source in this tree declares one and it ships disabled, so an ELN corpus — which declares none
    — was never labelled under any configuration, and the pass reported `has_more=False` while it
    happened, which reads as "nothing left to do".

    A block says what a source *carries*. It is read per row, as a policy, and never as permission.
    """

    async def _run() -> None:
        index = InMemoryLabelIndex()
        await index.record(_row("e1", source="eln-json"))
        await index.record(_row("p1", source="pistachio"))
        # What `label_policies()` returns today: only Pistachio declares a block.
        policies = {"pistachio": LabelPolicy()}

        report = await label_stale(index, _FakeLabeller(), policies, _VERSION, limit=10)

        assert report.labelled == 2
        assert {row.source for row in await index.stale("next-version", limit=10)} == {
            "eln-json",
            "pistachio",
        }
        assert await index.stale(_VERSION, limit=10) == []

    asyncio.run(_run())


def test_two_sources_sharing_a_reaction_id_each_keep_their_own_labels() -> None:
    """One batch, one id, two rows — and neither may be given the other's chemistry.

    `reaction_labels` keys on `(source, reaction_id)` precisely because two ELNs may use one entry
    id, and `stale()` spans sources, so a batch can hold both. Keying the labeller's answers on the
    bare id let the second overwrite the first: an esterification was stored with an amination's
    atom map and named reaction, `merge._species` applied the wrong species list positionally, and
    the pass reported both rows cleanly labelled.

    The fake answers each id it is handed, so a mismatch here can only come from the drain.
    """
    ester = "CCO.CC(=O)O>>CCOC(C)=O"

    async def _run() -> None:
        index = InMemoryLabelIndex()
        await index.record(_row("RXN-1", source="eln-a", record_smiles=ester))
        await index.record(_row("RXN-1", source="eln-b"))

        labeller = _FakeLabeller()
        report = await label_stale(index, labeller, {}, _VERSION, limit=10)

        assert report.labelled == 2 and report.unlabelled == 0
        # Distinct ids went on the wire, which is what makes two answers possible at all.
        assert len(set(labeller.sent[0])) == 2
        rows = {r.source: r for r in await index.stale("next-version", limit=10)}
        assert rows["eln-a"].record_smiles == ester
        assert rows["eln-b"].record_smiles == _RECORD
        # Each row carries the answer minted for its own id, not its neighbour's.
        for row in rows.values():
            assert row.mapped_smiles is not None
            assert row.named_reaction == "Buchwald-Hartwig amination"
            assert [s.derived_role for s in row.species][2] is SpeciesRole.LIGAND

    asyncio.run(_run())
