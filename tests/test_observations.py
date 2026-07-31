"""The ungated observations tier: what it may notice, and what it may never count (D-161).

Knowledge has had one tier and one gate, and the gate is right for anything asserted as fact. It is
also why there is no proactive cross-project learning loop: every candidate learning would cost a
reviewer a PR, and most do not earn one. An observation is explicitly not truth, so it does not
need the gate — the gate moves to the few worth promoting.

That only holds if two rules hold, and both are tested here rather than documented. Support must
count distinct *merged* notes, or the agent writes an observation, later counts its own
observation as corroboration, and inflates into a PR — a self-confirming loop that looks exactly
like cross-project evidence from outside. And an observation must never enter the evidence list,
or "what the record shows" and "what the agent noticed" become the same kind of thing at ranking
time.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import Component, OrdReaction, OutcomeClass, Role
from chemclaw.kg.note import Note
from chemclaw.memory.observation_mining import mine_corpus, mine_interactions
from chemclaw.memory.observations import Observation

_ESTER = ("CCO", "CC(=O)O", "CCOC(C)=O")


def _reaction(reaction_id: str, project: str, outcome: OutcomeClass) -> OrdReaction:
    """One esterification, so every fixture reaction lands in a single similarity cluster."""
    return OrdReaction(
        reaction_id=reaction_id,
        inputs=[
            Component(smiles=_ESTER[0], role=Role.REACTANT),
            Component(smiles=_ESTER[1], role=Role.REACTANT),
        ],
        outcomes=[Component(smiles=_ESTER[2], role=Role.PRODUCT)],
        provenance=f"test:{reaction_id}",
        project=project,
        outcome_class=outcome,
        failure_reason="decomposed on workup" if outcome is OutcomeClass.FAILURE else None,
    )


class TestTheAntiFeedbackRule:
    """The dangerous failure mode, refused at the point an observation is built."""

    def test_an_observation_may_not_cite_an_observation(self) -> None:
        """Otherwise the agent corroborates itself into a promotion, and it reads as evidence."""
        with pytest.raises(ValueError, match="merged notes"):
            Observation(
                statement="Acids do badly here.",
                scope="transformation:x",
                evidence_note_ids=["reaction-1", "observation-abc123"],
            )

    def test_support_is_derived_from_the_evidence_not_stored(self) -> None:
        """A counter can be incremented by something that is not a merged note. A count cannot."""
        observation = Observation(
            statement="s", scope="t", evidence_note_ids=["reaction-1", "reaction-2"]
        )
        assert observation.support == 2
        assert not hasattr(observation, "support_count")

    def test_the_id_is_derived_from_content_so_re_mining_accumulates(self) -> None:
        """A fresh id every night would make support meaningless and the table unbounded."""
        first = Observation(statement="s", scope="t", evidence_note_ids=["reaction-1"]).with_id()
        again = Observation(statement="s", scope="t", evidence_note_ids=["reaction-9"]).with_id()
        assert first.id == again.id
        assert first.id.startswith("observation-")
        other = Observation(statement="s", scope="u").with_id()
        assert other.id != first.id


class TestTheCorpusMiner:
    """It picks up precisely what the playbook bar throws away, and only that."""

    def test_a_cross_project_failure_cluster_becomes_an_observation(self) -> None:
        """The signal the playbook path must discard and this tier can hold.

        `find_playbook_candidates` keeps successes only, and correctly — distilling a recurring
        failure into a playbook would invert what the record says. But that drops the *finding*
        along with the recommendation, and "this went badly in two projects" is exactly what a
        process chemist wants to know before trying it in a third.
        """
        found = mine_corpus(
            [
                _reaction("r1", "alpha", OutcomeClass.FAILURE),
                _reaction("r2", "beta", OutcomeClass.FAILURE),
            ]
        )
        assert len(found) == 1
        assert found[0].projects_seen == ["alpha", "beta"]
        assert found[0].evidence_note_ids == ["reaction-r1", "reaction-r2"]
        assert found[0].origin == "corpus-mining"
        assert "failure" in found[0].statement

    def test_a_successful_cluster_is_left_to_the_playbook_layer(self) -> None:
        """Two tiers must not both hold the same finding, or a reviewer sees it twice."""
        assert (
            mine_corpus(
                [
                    _reaction("r1", "alpha", OutcomeClass.SUCCESS),
                    _reaction("r2", "beta", OutcomeClass.SUCCESS),
                ]
            )
            == []
        )

    def test_one_project_repeating_itself_is_not_an_observation(self) -> None:
        """That is episodic, and the campaign layer already covers it."""
        assert (
            mine_corpus(
                [
                    _reaction("r1", "alpha", OutcomeClass.FAILURE),
                    _reaction("r2", "alpha", OutcomeClass.FAILURE),
                ]
            )
            == []
        )

    def test_mining_is_deterministic(self) -> None:
        """A workflow re-runs, and an unstable miner would mint a new row every night."""
        corpus = [
            _reaction("r1", "alpha", OutcomeClass.FAILURE),
            _reaction("r2", "beta", OutcomeClass.INCONCLUSIVE),
        ]
        first = [o.with_id().id for o in mine_corpus(corpus)]
        again = [o.with_id().id for o in mine_corpus(list(reversed(corpus)))]
        assert first == again


class TestTheInteractionMiner:
    """The half that answers "what have chemists actually asked" — soundly."""

    def test_an_interaction_whose_evidence_spans_projects_is_observed(self) -> None:
        """The transfer already happened, in one conversation, where nobody else can see it."""
        notes = [
            Note(
                id="interaction-42",
                type="interaction",
                created_by="agent",
                body=(
                    "Q: does this hold?\n\nA: yes.\n\n"
                    "Evidence:\n- [[reaction-r1]]\n- [[reaction-r2]]\n"
                ),
            )
        ]
        reactions = [
            _reaction("r1", "alpha", OutcomeClass.SUCCESS),
            _reaction("r2", "beta", OutcomeClass.SUCCESS),
        ]
        found = mine_interactions(notes, reactions)
        assert len(found) == 1
        assert found[0].origin == "interaction"
        assert found[0].projects_seen == ["alpha", "beta"]
        # The interaction note *and* its cited reactions — all merged, all legitimate support.
        assert found[0].evidence_note_ids == ["interaction-42", "reaction-r1", "reaction-r2"]

    def test_an_interaction_inside_one_project_is_not_a_cross_project_finding(self) -> None:
        """Every confirmed answer would otherwise become an observation, which is just a log."""
        notes = [
            Note(
                id="interaction-1",
                type="interaction",
                created_by="agent",
                body="Q: ?\n\nA: .\n\nEvidence:\n- [[reaction-r1]]\n",
            )
        ]
        assert mine_interactions(notes, [_reaction("r1", "alpha", OutcomeClass.SUCCESS)]) == []

    def test_other_note_types_are_not_mined(self) -> None:
        """A playbook already crossed projects by construction; observing it says nothing new."""
        notes = [
            Note(
                id="playbook-x",
                type="playbook",
                created_by="agent",
                body="Evidence:\n- [[reaction-r1]]\n- [[reaction-r2]]\n",
            )
        ]
        reactions = [
            _reaction("r1", "alpha", OutcomeClass.SUCCESS),
            _reaction("r2", "beta", OutcomeClass.SUCCESS),
        ]
        assert mine_interactions(notes, reactions) == []


def test_the_recall_tool_is_silent_while_the_tier_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off by default: the first knowledge surface with no human gate is a deployment's choice.

    And "off" must mean the tool touches no database, not that it returns an error — an agent that
    calls it on an unconfigured deployment should simply learn there is nothing to recall.
    """
    from chemclaw.agent import memory_tools

    async def _run() -> None:
        monkeypatch.setattr(settings, "observations_enabled", False)
        assert await memory_tools.recall_observations() == []

    asyncio.run(_run())


def test_the_migration_forbids_self_citation_in_sql_too() -> None:
    """The model check is a courtesy; this one is the guarantee.

    A validator protects the path that goes through the model. The constraint protects the table,
    including from a future writer that does not.
    """
    sql = Path("infra/sql/025_observations.sql").read_text(encoding="utf-8")
    assert "observations_evidence_is_merged_notes" in sql
    assert "NOT LIKE '%observation-%'" in sql
