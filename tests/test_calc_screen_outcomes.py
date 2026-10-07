"""Every item of a screen ends as an outcome: an answer, or a failure that names it.

Bond, reaction-media and species-media screens are sets of independent answers, so a refused item
is reported beside the rest. A species ranking normalises populations over the whole set, so it
refuses, naming every form it could not compute. The boundary is `ValueError` only: each item
failure has a sibling test showing a backend outage still propagates. Refusals are driven through
`FakeCalcServer.overrides`, down the real wire path.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from pydantic import BaseModel
from rdkit import Chem

from chemclaw.connectors.calc import compose
from chemclaw.connectors.calc.remote import CalcBusyError, CalcServerError, CalcTimeBudgetError
from chemclaw.core.mcp_session import (
    SERVER_AT_CAPACITY,
    SERVER_INTERNAL_ERROR,
    SERVER_TIME_BUDGET,
)
from chemclaw.science.calc.models import (
    BondDissociationSurvey,
    FailedMedium,
    SolventComparisonResult,
    SpeciesSolventComparison,
)
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install

_ETHYLBENZENE = "CCc1ccccc1"
# Two C-C homolyses of ethylbenzene; the first is the one the tests below make fail.
_CLEAVAGES = [
    ((0, 1), "C-C", ["[CH2]c1ccccc1", "[CH3]"]),
    ((1, 2), "C-C", ["[CH2]C", "[c]1ccccc1"]),
]
_ESTERIFICATION = (["CC(=O)O", "CCO"], ["CC(=O)OCC", "O"])
_ESTER_SIGMAS = {"CC(=O)O": 1, "CCO": 1, "CC(=O)OCC": 1, "O": 2}
_KETO, _ENOL = "CC(=O)CC(C)=O", "CC(O)=CC(C)=O"
_TAUTOMERS = [(_KETO, "keto"), (_ENOL, "enol")]
_REFUSAL = "no parameters for this input on the server"


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion, the shape every test here uses."""
    return asyncio.run(coroutine)


def _refuse(
    server: FakeCalcServer,
    tool: str,
    when: Callable[[dict[str, Any]], bool],
    message: str = _REFUSAL,
) -> None:
    """Make `tool` refuse the calls `when` selects, and answer every other call as before.

    Raising `ValueError` is how the fake sends a refused call over the wire; the message's head
    decides what the client makes of it, which is how one helper produces all three failures.
    """
    answer = getattr(server, f"_{tool}")

    def refusing(arguments: dict[str, Any]) -> dict[str, Any]:
        if when(arguments):
            raise ValueError(message)
        result: dict[str, Any] = answer(arguments)
        return result

    server.overrides[tool] = refusing


def _embedding(*smiles: str) -> Callable[[dict[str, Any]], bool]:
    """Select the embedding of any of these SMILES: where every species' calculation begins."""
    return lambda arguments: arguments["smiles"] in smiles


def _relaxing(smiles: str | None = None, solvent: str | None = None) -> Callable[..., bool]:
    """Select a relaxation by species, by medium, or by both."""
    # A relaxed structure carries the canonical SMILES, not the string the caller wrote.
    wanted = None if smiles is None else Chem.MolToSmiles(Chem.MolFromSmiles(smiles))

    def selected(arguments: dict[str, Any]) -> bool:
        species = wanted is None or arguments["structure"].get("smiles") == wanted
        medium = solvent is None or arguments.get("solvent") == solvent
        return species and medium

    return selected


# --- the bond survey ---------------------------------------------------------------------------


def test_a_refused_bond_is_reported_beside_the_bonds_that_were_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bond the server refuses is that bond's answer, not the survey's.

    `considered == bonds + failed`: every bond asked about is in exactly one list. The weakest bond
    is flagged as weakest of the rest, with a warning that the refused one may be weaker.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding("[CH3]"))

    survey = _run(compose.bond_dissociation_survey(InMemoryStore(), _ETHYLBENZENE, _CLEAVAGES))

    assert survey.considered == len(survey.bonds) + len(survey.failed) == 2
    assert [bond.atoms for bond in survey.bonds] == [[1, 2]]
    assert survey.bonds[0].is_weakest
    (failed,) = survey.failed
    assert (failed.atoms, failed.fragments) == ([0, 1], ["[CH2]c1ccccc1", "[CH3]"])
    assert _REFUSAL in failed.reason, "the server's own sentence is the reason"
    (warning,) = [w for w in survey.warnings if "could not be computed" in w]
    assert "C-C [0, 1]" in warning, "a bond is named with its atoms — 'C-C' alone is ambiguous"
    assert "may be weaker" in warning


def test_a_survey_in_which_no_bond_could_be_computed_refuses_naming_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No answer is not an empty ranking: the job fails, and says which bond failed and why."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding("[CH3]", "[CH2]C"))

    with pytest.raises(ValueError, match="no bond of") as refused:
        _run(compose.bond_dissociation_survey(InMemoryStore(), _ETHYLBENZENE, _CLEAVAGES))
    assert "C-C [0, 1]" in str(refused.value)
    assert "C-C [1, 2]" in str(refused.value)


@pytest.mark.parametrize(
    ("head", "outage"),
    [(SERVER_INTERNAL_ERROR, CalcServerError), (SERVER_AT_CAPACITY, CalcBusyError)],
)
def test_an_outage_during_a_survey_is_not_reported_as_a_failed_bond(
    monkeypatch: pytest.MonkeyPatch, head: str, outage: type[Exception]
) -> None:
    """A fault or a full pod says nothing about the bond, so it reaches Temporal as itself.

    Both are retryable `SubsystemUnavailableError`s; folding them into `failed` would complete a
    survey that silently omits a bond.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding("[CH3]"), message=f"{head} try later")

    with pytest.raises(outage):
        _run(compose.bond_dissociation_survey(InMemoryStore(), _ETHYLBENZENE, _CLEAVAGES))


# --- the reaction solvent screen -------------------------------------------------------------


def test_a_refused_medium_is_reported_and_the_rest_are_still_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A medium the server refuses while computing costs that row only.

    Unparameterised solvents are refused before launch by
    `science/calc/solvents.require_supported_solvents`, so this is a server-side refusal.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(solvent="toluene"))

    result = _run(
        compose.solvent_comparison(
            InMemoryStore(),
            *_ESTERIFICATION,
            ["water", "toluene"],
            symmetry_numbers=_ESTER_SIGMAS,
        )
    )

    assert sorted(str(effect.solvent) for effect in result.effects) == ["None", "water"]
    assert [entry.solvent for entry in result.failed] == ["toluene"]
    assert _REFUSAL in result.failed[0].reason
    assert result.best_solvent != "toluene"
    (lost,) = [w for w in result.warnings if "could not be computed" in w]
    assert "1 of 3 media" in lost and "toluene" in lost


def test_a_screen_left_with_one_medium_does_not_claim_the_media_are_indistinguishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spread over one row is zero by construction, not a finding about the solvents.

    The "does not distinguish them" sentence is a verdict on a comparison, and with one medium left
    no comparison happened — so it says there is nothing to compare instead.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(solvent="water"))

    result = _run(
        compose.solvent_comparison(
            InMemoryStore(), *_ESTERIFICATION, ["water"], symmetry_numbers=_ESTER_SIGMAS
        )
    )

    assert [effect.solvent for effect in result.effects] == [None]
    assert not [w for w in result.warnings if "does not distinguish" in w]
    assert any("nothing to compare" in w for w in result.warnings)


def test_a_solvent_screen_in_which_every_medium_is_refused_names_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every medium refused is no answer, and the refusal says which media and why."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing())

    with pytest.raises(ValueError, match="no medium of this solvent screen") as refused:
        _run(
            compose.solvent_comparison(
                InMemoryStore(), *_ESTERIFICATION, ["water"], symmetry_numbers=_ESTER_SIGMAS
            )
        )
    assert "gas phase" in str(refused.value)
    assert "water" in str(refused.value)


def test_an_outage_in_one_medium_fails_the_screen_rather_than_the_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-medium boundary is inside the `gather`, so an outage still leaves through it."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(
        server,
        "relax_structure",
        _relaxing(solvent="toluene"),
        message=f"{SERVER_AT_CAPACITY} 0 of 4 slots free",
    )

    with pytest.raises(CalcBusyError):
        _run(
            compose.solvent_comparison(
                InMemoryStore(),
                *_ESTERIFICATION,
                ["water", "toluene"],
                symmetry_numbers=_ESTER_SIGMAS,
            )
        )


def test_a_payload_the_client_cannot_validate_fails_the_screen_rather_than_one_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contract skew only some inputs reach is the server's bug, not a medium the server refused.

    pydantic's `ValidationError` is a `ValueError`, so it must not be recorded per item as one
    medium "refused"; it fails the screen.
    """
    from pydantic import ValidationError

    server = install(monkeypatch, FakeCalcServer())
    answer = server._relax_structure
    in_toluene = _relaxing(solvent="toluene")

    def skewed(arguments: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = answer(arguments)
        return {"unexpected": True} if in_toluene(arguments) else result

    server.overrides["relax_structure"] = skewed

    with pytest.raises(ValidationError):
        _run(
            compose.solvent_comparison(
                InMemoryStore(),
                *_ESTERIFICATION,
                ["water", "toluene"],
                symmetry_numbers=_ESTER_SIGMAS,
            )
        )


def test_an_outage_in_one_medium_stops_the_others_and_is_raised_as_itself() -> None:
    """The siblings of a failing medium are cancelled, and the error is not wrapped.

    `asyncio.gather` alone leaves them running; `asyncio.TaskGroup` would raise an `ExceptionGroup`
    that `durable/publish.py`'s retry classification does not recognise. Both halves are asserted.
    """
    stopped: list[str] = []

    async def outage() -> None:
        await asyncio.sleep(0)
        raise CalcBusyError("0 of 4 slots free")

    async def a_slow_medium(name: str) -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            stopped.append(name)
            raise
        return name

    stopped_when_raised: list[str] = []

    async def screen() -> list[Any]:
        try:
            return await compose._every_medium(
                [a_slow_medium("water"), outage(), a_slow_medium("dmso")]
            )
        except CalcBusyError:
            # Read here, not after `asyncio.run` returns: shutting the loop down cancels whatever
            # is still pending, which would make a leaking implementation look like this one.
            stopped_when_raised.extend(stopped)
            raise

    started = time.monotonic()
    with pytest.raises(CalcBusyError):
        _run(screen())
    assert time.monotonic() - started < 5, "the screen waited for media it had already lost"
    assert sorted(stopped_when_raised) == ["dmso", "water"]


def test_media_that_all_answer_come_back_in_the_order_they_were_asked() -> None:
    """Order is what keeps the gas-phase reference first; the cancelling wrapper must keep it."""

    async def medium(name: str, delay: float) -> str:
        await asyncio.sleep(delay)
        return name

    async def screen() -> list[str]:
        return await compose._every_medium(
            [medium("gas", 0.03), medium("water", 0.0), medium("dmso", 0.01)]
        )

    assert _run(screen()) == ["gas", "water", "dmso"]


# --- the species ranking and its solvent screen -------------------------------------------------


def test_a_ranking_with_a_refused_form_refuses_after_trying_every_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ranking with a refused form refuses after trying every form.

    The refusal names the form, and every other form is computed (and cached) first, so the rerun
    pays nothing twice. The refused form is first, so stopping at the first failure would fail this.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding(_KETO))
    store = InMemoryStore()

    with pytest.raises(ValueError, match="1 of 2 species could not be computed") as refused:
        _run(compose.species_ranking(store, _TAUTOMERS, kind="tautomers"))
    assert _KETO in str(refused.value)
    assert _REFUSAL in str(refused.value)
    enol = Chem.MolToSmiles(Chem.MolFromSmiles(_ENOL))
    relaxed = server.count("relax_structure")
    assert any(a["structure"]["smiles"] == enol for a in server.arguments("relax_structure")), (
        "the enol, after the refused keto form, was still computed"
    )

    _run(compose.species_ranking(store, [(_ENOL, "enol")], kind="tautomers"))
    assert server.count("relax_structure") == relaxed, "the rerun recomputed nothing"


def test_a_ranking_names_every_refused_form_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing at the first form made the chemist find the second one on the next run."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding(_KETO, _ENOL))

    with pytest.raises(ValueError, match="2 of 2 species") as refused:
        _run(compose.species_ranking(InMemoryStore(), _TAUTOMERS, kind="tautomers"))
    assert _KETO in str(refused.value)
    assert _ENOL in str(refused.value)


def test_a_full_pod_during_a_ranking_stays_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CalcBusyError` must leave a ranking as itself, not as a refused species."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding(_ENOL), message=f"{SERVER_AT_CAPACITY} 0 free")

    with pytest.raises(CalcBusyError):
        _run(compose.species_ranking(InMemoryStore(), _TAUTOMERS, kind="tautomers"))


def test_a_species_screen_reports_the_medium_where_a_form_failed_and_ranks_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A form that fails in one medium costs that whole medium and no other.

    Every reported distribution ranks both forms, and every response has one standing per reported
    medium.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(smiles=_ENOL, solvent="toluene"))

    screen = _run(
        compose.species_solvent_comparison(
            InMemoryStore(), _TAUTOMERS, ["water", "toluene"], kind="tautomers"
        )
    )

    assert [d.solvent for d in screen.distributions] == [None, "water"]
    assert all(len(d.species) == 2 for d in screen.distributions)
    (failed,) = screen.failed
    assert failed.solvent == "toluene"
    assert _ENOL in failed.reason, "the medium's reason names the form that failed in it"
    for response in screen.responses:
        assert [standing.solvent for standing in response.standings] == [None, "water"]
    assert any("toluene" in w and "could not be computed" in w for w in screen.warnings)


def test_a_species_screen_with_no_species_is_one_refusal_not_one_per_medium() -> None:
    """Every medium would refuse an empty set identically; one sentence is the answer."""
    with pytest.raises(ValueError, match="at least one species"):
        _run(compose.species_solvent_comparison(InMemoryStore(), [], ["water"], kind="tautomers"))


# --- the wire ------------------------------------------------------------------------------------


_BUILDERS: dict[str, Callable[[InMemoryStore], Coroutine[Any, Any, BaseModel]]] = {
    "survey": lambda store: compose.bond_dissociation_survey(store, _ETHYLBENZENE, _CLEAVAGES),
    "solvents": lambda store: compose.solvent_comparison(
        store, *_ESTERIFICATION, ["water"], symmetry_numbers=_ESTER_SIGMAS
    ),
    "species": lambda store: compose.species_solvent_comparison(
        store, _TAUTOMERS, ["water"], kind="tautomers"
    ),
}


@pytest.mark.parametrize("screen", sorted(_BUILDERS))
def test_a_payload_written_before_failed_existed_still_decodes(
    monkeypatch: pytest.MonkeyPatch, screen: str
) -> None:
    """These are Temporal wire types, so a run in flight across the deploy must still decode.

    Built from a real result and dumped *without* the new field, rather than from a literal, so
    the test tracks the model instead of a copy of it.
    """
    install(monkeypatch, FakeCalcServer())
    result = _run(_BUILDERS[screen](InMemoryStore()))
    assert isinstance(
        result, BondDissociationSurvey | SolventComparisonResult | SpeciesSolventComparison
    )
    old_shape = result.model_dump(mode="json", exclude={"failed"})

    assert "failed" not in old_shape
    assert type(result).model_validate(old_shape).failed == []


def test_a_failed_medium_names_the_gas_phase_as_none() -> None:
    """`solvent=None` is the gas-phase reference, the convention every row in these models uses."""
    assert FailedMedium(solvent=None, reason="x").solvent is None


# --- what the review of the first cut found -------------------------------------------------------


def test_a_survey_computes_its_parent_once_and_reads_it_back_for_every_bond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent is computed up front and every bond's reaction must find it in the cache.

    One relaxation of the parent across a two-bond survey is what proves the up-front computation
    and the per-bond one share a key: if the settings ever diverged, the bonds would relax it again.
    """
    server = install(monkeypatch, FakeCalcServer())

    _run(compose.bond_dissociation_survey(InMemoryStore(), _ETHYLBENZENE, _CLEAVAGES))

    parent = Chem.MolToSmiles(Chem.MolFromSmiles(_ETHYLBENZENE))
    relaxed = [a for a in server.arguments("relax_structure") if a["structure"]["smiles"] == parent]
    assert len(relaxed) == 1


def test_a_refused_parent_fails_the_survey_once_rather_than_once_per_bond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent is the survey's input, not one of its items, and a refusal is not cached.

    As an item it was asked for — and refused — once per bond, each attempt possibly minutes of
    server time, and the error repeated one reason N times.
    """
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding(_ETHYLBENZENE))

    with pytest.raises(ValueError, match=_REFUSAL) as refused:
        _run(compose.bond_dissociation_survey(InMemoryStore(), _ETHYLBENZENE, _CLEAVAGES))

    asked = [a for a in server.arguments("embed_structure") if a["smiles"] == _ETHYLBENZENE]
    assert len(asked) == 1
    assert str(refused.value).count(_REFUSAL) == 1


def test_a_one_medium_screen_nothing_failed_in_does_not_report_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One solvent over an ionic set has no gas reference, and that is not a lost comparison.

    The "only one medium could be computed" sentence asserts that something failed; with `failed`
    empty it would be telling the chemist about a failure that never happened.
    """
    install(monkeypatch, FakeCalcServer())

    screen = _run(
        compose.species_solvent_comparison(
            InMemoryStore(),
            [("CC(=O)O", "acid"), ("CC(=O)[O-]", "acetate")],
            ["water"],
            kind="microstates",
            level="quick",
        )
    )

    assert screen.failed == []
    assert not [w for w in screen.warnings if "could be computed" in w]


def test_an_equation_the_screen_refuses_is_refused_once_not_once_per_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every medium runs the same equation, so its own checks run before the fan-out.

    Otherwise a mistyped sigma key came back as "no medium could be computed" followed by the same
    sentence once per medium.
    """
    install(monkeypatch, FakeCalcServer())

    with pytest.raises(ValueError, match="symmetry_numbers names species") as refused:
        _run(
            compose.solvent_comparison(
                InMemoryStore(),
                *_ESTERIFICATION,
                ["water", "toluene"],
                symmetry_numbers={**_ESTER_SIGMAS, "OCC": 1},
            )
        )
    assert "no medium" not in str(refused.value)


def test_a_gas_row_alone_is_not_described_against_solvent_rows_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standard-state caveat compares the gas row with solution rows; with none it is moot."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(solvent="water"))

    result = _run(
        compose.solvent_comparison(
            InMemoryStore(),
            ["C=C", "C=C"],
            ["C1CCC1"],
            ["water"],
            symmetry_numbers={"C=C": 4, "C1CCC1": 8},
        )
    )

    assert [effect.solvent for effect in result.effects] == [None]
    assert not [w for w in result.warnings if "standard state" in w]


def test_a_species_screen_in_which_every_medium_is_refused_names_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every medium refused is no answer, and the refusal says which media and why."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(smiles=_ENOL))

    with pytest.raises(ValueError, match="no medium of this species screen") as refused:
        _run(
            compose.species_solvent_comparison(
                InMemoryStore(), _TAUTOMERS, ["water"], kind="tautomers"
            )
        )
    assert "gas phase" in str(refused.value)
    assert "water" in str(refused.value)


def test_an_outage_in_one_medium_fails_the_species_screen_rather_than_the_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ranking inside the medium and the medium's own boundary both let an outage through."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(
        server,
        "relax_structure",
        _relaxing(solvent="toluene"),
        message=f"{SERVER_INTERNAL_ERROR} while relaxing",
    )

    with pytest.raises(CalcServerError):
        _run(
            compose.species_solvent_comparison(
                InMemoryStore(), _TAUTOMERS, ["water", "toluene"], kind="tautomers"
            )
        )


def test_a_species_screen_left_with_one_medium_makes_no_comparison_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swing over one medium is zero by construction, not a finding about the media."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(solvent="water"))

    screen = _run(
        compose.species_solvent_comparison(InMemoryStore(), _TAUTOMERS, ["water"], kind="tautomers")
    )

    assert [d.solvent for d in screen.distributions] == [None]
    assert not [w for w in screen.warnings if "does not distinguish" in w]
    assert any("nothing to compare" in w for w in screen.warnings)


# --- a stop by the server's clock is named, not mistaken for a refused item ----------------------

_STOPPED = f"{SERVER_TIME_BUDGET} a geometry optimization exceeded this server's inline budget"


def test_a_medium_the_clock_stopped_is_recorded_as_a_time_budget_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same medium may pass on an idle pod, so it is not listed as a refused input.

    And a medium refused for its input beside it stays `refused`: the cause is read off each
    refusal, so neither default can stand in for the other.
    """
    server = install(monkeypatch, FakeCalcServer())

    def stopped_or_refused(arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("solvent") == "toluene":
            raise ValueError(_STOPPED)
        if arguments.get("solvent") == "methanol":
            raise ValueError(_REFUSAL)
        result: dict[str, Any] = server._relax_structure(arguments)
        return result

    server.overrides["relax_structure"] = stopped_or_refused

    result = _run(
        compose.solvent_comparison(
            InMemoryStore(),
            *_ESTERIFICATION,
            ["water", "toluene", "methanol"],
            symmetry_numbers=_ESTER_SIGMAS,
        )
    )

    assert [(entry.solvent, entry.cause) for entry in result.failed] == [
        ("toluene", "time_budget"),
        ("methanol", "refused"),
    ]


def test_a_bond_the_clock_stopped_is_recorded_as_a_time_budget_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A survey records the stop per bond, as a screen does per medium."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding("[CH3]"), message=_STOPPED)

    survey = _run(compose.bond_dissociation_survey(InMemoryStore(), _ETHYLBENZENE, _CLEAVAGES))

    assert [entry.cause for entry in survey.failed] == ["time_budget"]


def test_a_ranking_every_failure_of_which_was_the_clock_is_named_as_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a species screen that makes the medium a `time_budget` stop rather than refused."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(smiles=_ENOL, solvent="toluene"), message=_STOPPED)

    with pytest.raises(CalcTimeBudgetError):
        _run(
            compose.species_ranking(
                InMemoryStore(), _TAUTOMERS, kind="tautomers", solvent="toluene"
            )
        )

    screen = _run(
        compose.species_solvent_comparison(
            InMemoryStore(), _TAUTOMERS, ["water", "toluene"], kind="tautomers"
        )
    )
    (stopped,) = screen.failed
    assert (stopped.solvent, stopped.cause) == ("toluene", "time_budget")


def test_a_ranking_with_any_refused_input_is_a_refusal_whatever_else_was_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One form the server cannot handle means no amount of waiting completes the set."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "embed_structure", _embedding(_KETO))
    _refuse_also = server.overrides["embed_structure"]

    def both(arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments["smiles"] == _ENOL:
            raise ValueError(_STOPPED)
        return _refuse_also(arguments)

    server.overrides["embed_structure"] = both

    with pytest.raises(ValueError, match="2 of 2 species") as refused:
        _run(compose.species_ranking(InMemoryStore(), _TAUTOMERS, kind="tautomers"))
    assert not isinstance(refused.value, CalcTimeBudgetError)


def test_a_screen_every_item_of_which_the_clock_stopped_is_a_stop_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing answered and no input was refused: the job says the clock stopped it."""
    server = install(monkeypatch, FakeCalcServer())
    _refuse(server, "relax_structure", _relaxing(), message=_STOPPED)

    with pytest.raises(CalcTimeBudgetError, match="no medium of this solvent screen"):
        _run(
            compose.solvent_comparison(
                InMemoryStore(), *_ESTERIFICATION, ["water"], symmetry_numbers=_ESTER_SIGMAS
            )
        )
