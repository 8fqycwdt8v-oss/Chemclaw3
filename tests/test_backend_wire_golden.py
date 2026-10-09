"""The bytes this repository sends to the `calc` and `rxnlabel` backends do not change unseen.

Every call site that puts a tool name and an argument dict on the wire is driven once against the
fakes, and what reached the wire is compared with `tests/fixtures/backend_wire_golden.json`. The
comparison is on canonical JSON text, so an `int` that became a `float`, a key that appeared or one
that went missing fails it; coordinates are rounded and geometry addresses are masked, because those
two depend on the RDKit build rather than on this repository.

Regenerate with `CHEMCLAW_REGENERATE_WIRE_GOLDEN=1`; a diff in review is the evidence that the
contract with the fleet moved.
"""

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from rdkit import Chem

import chemclaw.connectors.calc.server.tools as calc_tools
import chemclaw.ingest.labels.labeller as labeller
from chemclaw.connectors.bo.calculators import log_s_for, properties_for
from chemclaw.connectors.calc import compose
from chemclaw.core.chem import torsion_handle
from chemclaw.science.calc.models import Torsion
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install

GOLDEN = Path(__file__).parent / "fixtures" / "backend_wire_golden.json"

#: Coordinates are compared to this many decimals: the last digits of an embedding are the RDKit
#: build's, not ours.
_DECIMALS = 2

#: The one `Structure` field that is a digest of the coordinates.
_MASKED = "structure_id"


def _normal(value: Any) -> Any:
    """`value` with floats rounded and geometry addresses masked, types otherwise preserved."""
    if isinstance(value, dict):
        return {
            key: "<masked>" if key == _MASKED and item else _normal(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normal(item) for item in value]
    if isinstance(value, float):
        return round(value, _DECIMALS)
    return value


def _canonical(tool: str, arguments: dict[str, Any]) -> str:
    """One request as the canonical JSON text compared against the golden."""
    return json.dumps({"tool": tool, "arguments": _normal(arguments)}, sort_keys=True)


def _dihedral_torsion() -> Torsion:
    """n-butane's central bond, as `enumerate_torsions` reports it."""
    return Torsion.model_validate(
        {
            "torsion_id": torsion_handle(Chem.MolFromSmiles("CCCC"), (1, 2)),
            "atoms": [0, 1, 2, 3],
            "bond": [1, 2],
            "label": "the C1-C2 bond",
            "symmetry_order": 1,
            "period_degrees": 360.0,
        }
    )


async def _composites(server: FakeCalcServer) -> None:
    """Every `compose` site that sends: embed, relax, Hessian, scans, searches, averages."""
    del server
    store = InMemoryStore()
    for solvent in (None, "water"):
        structure = await compose.embed("CCO")
        await compose.relax(store, structure, solvent)
        await compose.hessian(store, structure, solvent)
        await compose.relax_to_minimum(store, structure, solvent)
        await compose.scan_profile(store, "CCCC", (0, 1, 2, 3), (0.0, 60.0, 120.0), solvent)
    await compose.embed("[CH3]")
    for search in ("conformers", "protomers"):
        await compose.conformer_ensemble(store, "CCCC", search=search, effort="quick")
    await compose.conformer_ensemble(store, "CCCC", search="conformers", effort="extensive")
    await compose.interaction(store, "O", "CO")
    await compose.ensemble_property(store, "CCO", prop="dipole_debye")
    await compose.ensemble_property(store, "CCO", prop="fukui")


async def _rotation(server: FakeCalcServer) -> None:
    """The three scans a rotational profile makes: the sweep, the maxima and the pass geometry."""
    del server
    await compose.rotation_profile(InMemoryStore(), "CCCC", _dihedral_torsion())


async def _tools(server: FakeCalcServer) -> None:
    """Every agent-facing calculator tool that reaches the backend, and the version probe."""
    del server
    geometry = await compose.embed("CCO")
    handle = geometry.structure_id
    assert handle, "the fake's geometry store minted no address, so the by-handle routes are unread"
    await calc_tools.compute_xtb_energy("CCO", charge=1)
    await calc_tools.predict_solubility("CCO")
    await calc_tools.predict_pka("CC(=O)O")
    await calc_tools.predict_developability_profile("CCO")
    await calc_tools.compute_electronic_properties("CCO")
    await calc_tools.compute_electronic_properties("CCO", solvent="water")
    await calc_tools.compute_electronic_properties("CCO", structure_id=handle)
    await calc_tools.predict_site_reactivity("CCO", mode="nucleophilic", top_n=3)
    await calc_tools.predict_site_reactivity("CCO", structure_id=handle)
    await calc_tools.optimize_geometry("CCO")
    await calc_tools.compute_thermochemistry("O")
    await calc_tools.predict_logd("CC(=O)O", ph=7.4)
    for tool in (calc_tools.compute_atomic_descriptors, calc_tools.compute_surface_potential):
        # The fake serves neither, so each fails after its request is recorded.
        with pytest.raises(Exception):  # noqa: B017
            await tool("CCO")
    for property_name in ("solubility", "pka"):
        await calc_tools._calibrated(property_name)
    store = InMemoryStore()
    await properties_for(store)("CCO")
    await log_s_for(store)("CCO")


@pytest.fixture
def recorded_labeller(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every `(tool, arguments)` the labeller hands its session, answered with a minimal payload."""
    sent: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def _session(*_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        yield object()

    async def _invoke(_session: object, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        sent.append((tool, arguments))
        return {"version": "fake-labeller", "results": []}

    monkeypatch.setattr(labeller, "open_session", _session)
    monkeypatch.setattr(labeller, "invoke", _invoke)
    return sent


async def _labels() -> None:
    """The three requests the corpus-labelling drain sends."""
    server = labeller.RxnLabelServer()
    await server.version()
    await server.represent(
        [("r1", "CC(=O)O.OCC>>CC(=O)OCC", ["CC(=O)O", "OCC"]), ("r2", "CC>>CC", [])]
    )
    await server.name([("r1", "CC(=O)O.OCC>>CC(=O)OCC"), ("r2", "CC>>CC")])


def _golden() -> dict[str, list[str]]:
    loaded: dict[str, list[str]] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def written() -> Iterator[dict[str, list[str]]]:
    """The requests recorded by this module's tests, written to the golden when asked to."""
    collected: dict[str, list[str]] = {}
    yield collected
    if os.environ.get("CHEMCLAW_REGENERATE_WIRE_GOLDEN"):
        existing = _golden() if GOLDEN.exists() else {}
        GOLDEN.write_text(
            json.dumps({**existing, **collected}, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _check(name: str, requests: list[str], written: dict[str, list[str]]) -> None:
    written[name] = requests
    if os.environ.get("CHEMCLAW_REGENERATE_WIRE_GOLDEN"):
        return
    assert requests, f"{name} sent nothing, so there is nothing to hold"
    assert requests == _golden()[name], (
        f"the {name} requests differ from tests/fixtures/backend_wire_golden.json: a change here "
        "is a change to what the fleet receives"
    )


async def test_the_composites_send_the_recorded_calc_requests(
    monkeypatch: pytest.MonkeyPatch, written: dict[str, list[str]]
) -> None:
    """Every request `connectors/calc/compose.py` makes, in order, byte for byte."""
    server = install(monkeypatch, FakeCalcServer())
    await _composites(server)
    _check("composites", [_canonical(tool, args) for tool, args in server.calls], written)


async def test_the_rotational_profile_sends_the_recorded_calc_requests(
    monkeypatch: pytest.MonkeyPatch, written: dict[str, list[str]]
) -> None:
    """The scans of `rotation_profile`, against the fake's three-well torsion."""
    server = install(monkeypatch, FakeCalcServer(torsion=(0, 1, 2, 3)))
    await _rotation(server)
    _check("rotation", [_canonical(tool, args) for tool, args in server.calls], written)


async def test_the_tools_send_the_recorded_calc_requests(
    monkeypatch: pytest.MonkeyPatch, written: dict[str, list[str]]
) -> None:
    """Every request the agent-facing calculators, the BO bindings and the version probe make."""
    monkeypatch.setattr(calc_tools, "default_store", lambda: InMemoryStore())

    async def _no_ledger(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(calc_tools, "_log_prediction", _no_ledger)
    server = install(monkeypatch, FakeCalcServer())
    await _tools(server)
    _check("tools", [_canonical(tool, args) for tool, args in server.calls], written)


async def test_the_labeller_sends_the_recorded_rxnlabel_requests(
    recorded_labeller: list[tuple[str, dict[str, Any]]], written: dict[str, list[str]]
) -> None:
    """The version probe and both batch calls of the drain, byte for byte."""
    await _labels()
    _check("labeller", [_canonical(tool, args) for tool, args in recorded_labeller], written)


def test_the_golden_names_every_tool_this_repository_sends() -> None:
    """The recorded tools are the ones the code is known to call; a missing one is unread."""
    golden = _golden()
    sent = {json.loads(line)["tool"] for lines in golden.values() for line in lines}
    assert {
        "calculation_key",
        "embed_structure",
        "combine_structures",
        "relax_structure",
        "compute_hessian",
        "scan_point",
        "search_conformer_ensemble",
        "search_binding_modes",
        "compute_properties_at",
        "compute_fukui_at",
        "compute_xtb_energy",
        "predict_solubility",
        "predict_pka",
        "compute_electronic_properties",
        "predict_site_reactivity",
        "predict_developability_profile",
        "compute_atomic_descriptors",
        "compute_surface_potential",
        "labeller_version",
        "represent_reactions",
        "name_reactions",
    } <= sent
