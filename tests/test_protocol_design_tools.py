"""The four tools an agent writes a protocol with, driven directly against a real store.

`InMemoryDesignStore` is a real backend rather than a double, so nothing here is mocked: the tools
run their own checks, their own layout arithmetic and their own append, and the assertions are about
what came back and what landed.

Two properties carry most of the weight. **Every return is JSON that round-trips through the model
it claims to be** — the front end does `JSON.parse` and renders *nothing* on a failure, so a tool
returning prose is a blank panel rather than an error. And **a design citing nothing is refused**,
which is the difference between a prompt asking for evidence and a system requiring it.

A third property now shapes every drafting test: **`draft_experiment_protocol` takes no ask.** It
reads the request out of the design `structure_experiment_request` opened and composes the
`ExperimentDesign` itself, so the intake is a hard prerequisite rather than documented advice —
which is why `_open` runs before every `_draft` below.
"""

import asyncio
import inspect
import json
from collections.abc import Iterator

import pytest

import chemclaw.agent.protocol_design_tools as tools
from chemclaw.agent.authz import require_actor
from chemclaw.core.errors import ChemclawError
from chemclaw.core.turn_text import (
    get_current_user_text,
    reset_current_user_text,
    set_current_user_text,
)
from chemclaw.protocols.models import (
    ChargeLine,
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    ProtocolArm,
    ProtocolBody,
    RequestField,
    Setpoints,
    design_id_for,
)
from chemclaw.protocols.render import DesignListing, ProtocolReadout, ProtocolReceipt
from chemclaw.protocols.store import InMemoryDesignStore

_SOURCE = (
    "We need to get the Suzuki on the deactivated chloride working. "
    "24 wells, no DMF, by Friday please."
)


@pytest.fixture(autouse=True)
def chemist_said() -> Iterator[None]:
    """The chemist's message for the turn, stamped the way `api.runner` stamps it.

    Autouse because every test in this file is a turn, and off a turn there is no chemist: the tool
    no longer takes the text as an argument (a haystack the model supplies is one the model can
    invent), so a test that stamped nothing would be testing the refusal path by accident.
    """
    token = set_current_user_text(_SOURCE)
    try:
        yield
    finally:
        reset_current_user_text(token)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryDesignStore]:
    """A fresh real store behind the tools, so no test inherits another's designs.

    `default_design_store` hands out a module-level singleton on purpose (a backend that forgot
    everything between two calls would be worse than none), which is exactly why a test must not
    use it: one test's designs would show up in another's listing.
    """
    fresh = InMemoryDesignStore()
    monkeypatch.setattr(tools, "_store", lambda: fresh)
    yield fresh


def _request(**overrides: object) -> ExperimentRequest:
    """The ask the sample text above states."""
    fields: dict[str, object] = {
        "title": "SM-3 Suzuki",
        "goal": "couple the deactivated aryl chloride",
    }
    fields.update(overrides)
    return ExperimentRequest.model_validate(fields)


def _cited() -> list[EvidenceRef]:
    return [
        EvidenceRef(kind="precedent", ref="reaction-1", summary="a run like this gave 72%"),
        EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
    ]


def _arms(count: int) -> list[ProtocolArm]:
    """`count` arms named `A1`..`An` — the least that clears `is_a_protocol`."""
    return [ProtocolArm(arm_id=f"A{index}") for index in range(1, count + 1)]


def _protocol(*, request: ExperimentRequest | None = None, arms: int = 1) -> ExperimentDesign:
    """The design the tool composes out of what `_draft` hands it.

    A separate builder from `_draft` rather than its argument, deliberately: the tool takes no
    `request`, so comparing a stored revision against this is what proves the ask came out of the
    store instead of out of the call.
    """
    return ExperimentDesign(
        request=request or _request(),
        arms=_arms(arms),
        evidence=_cited(),
    )


async def _open(request: ExperimentRequest | None = None) -> ProtocolReceipt:
    """Structure an ask, because a draft has nowhere to land without one."""
    return ProtocolReceipt.model_validate_json(
        await tools.structure_experiment_request(request or _request())
    )


async def _draft(
    design_id: str,
    parent_revision: int,
    *,
    arms: int | list[ProtocolArm] = 1,
    cited: bool = True,
    evidence: list[EvidenceRef] | None = None,
    change_note: str = "drafted the protocol",
    base: ProtocolBody | None = None,
    factors: list[Factor] | None = None,
    plate_format: int = 0,
    randomize_run_order: bool = False,
    seed: int | None = None,
) -> str:
    """Draft against an open design, passing the parts the tool assembles into one design.

    `arms` takes a count for the usual case and a list when the arms have to set factor levels,
    which is the only shape a screen's `factor_levels_declared` accepts.
    """
    return await tools.draft_experiment_protocol(
        design_id,
        parent_revision,
        base or ProtocolBody(),
        (_cited() if cited else []) if evidence is None else evidence,
        change_note,
        factors=factors,
        arms=_arms(arms) if isinstance(arms, int) else list(arms),
        plate_format=plate_format,
        randomize_run_order=randomize_run_order,
        seed=seed,
    )


# --- require_quotes_are_verbatim ----------------------------------------------------------------


def test_a_paraphrased_quote_is_refused_and_the_slot_is_named() -> None:
    """A paraphrase reaching the record as a quotation is worse than an unmarked inference."""
    request = _request(
        plate_format=RequestField(value="24", basis="stated", quote="a 24-well plate")
    )
    with pytest.raises(ChemclawError, match="plate_format"):
        tools.require_quotes_are_verbatim(request, _SOURCE)


def test_a_quote_differing_only_in_whitespace_and_case_is_accepted() -> None:
    """A quote re-wrapped across lines is the same words; only a paraphrase is the failure."""
    request = _request(
        plate_format=RequestField(value="24", basis="stated", quote="  24   WELLS\n"),
        deadline=RequestField(value="Friday", basis="stated", quote="by Friday"),
    )
    tools.require_quotes_are_verbatim(request, _SOURCE)


def test_an_inferred_or_absent_slot_is_never_asked_for_a_quote() -> None:
    """Inference is legitimate here — what is refused is an *unmarked* one."""
    request = _request(
        scale=RequestField(value="100 mg", basis="inferred"),
        max_runs=RequestField(basis="absent"),
    )
    tools.require_quotes_are_verbatim(request, _SOURCE)


def test_every_quoted_slot_is_checked_and_not_just_the_first() -> None:
    """One correct quote must not vouch for a wrong one beside it."""
    request = _request(
        plate_format=RequestField(value="24", basis="stated", quote="24 wells"),
        scale=RequestField(value="5 g", basis="stated", quote="five grams"),
    )
    with pytest.raises(ChemclawError, match="scale"):
        tools.require_quotes_are_verbatim(request, _SOURCE)


# --- structure_experiment_request ---------------------------------------------------------------


def test_structuring_a_request_stores_revision_one_as_a_request(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        request = _request(project="prj-a")
        payload = await tools.structure_experiment_request(request)

        receipt = ProtocolReceipt.model_validate_json(payload)
        assert receipt.design_id == design_id_for(request, owner=require_actor())
        assert receipt.revision == 1
        assert receipt.status == "requested"
        assert receipt.title == "SM-3 Suzuki"

        stored = await store.read(receipt.design_id)
        assert stored is not None
        assert stored.kind == "request"
        assert stored.author_kind == "agent"
        assert stored.design.request == request
        assert stored.change_note == "structured the request"

    asyncio.run(_body())


def test_a_structured_request_returns_json_the_front_end_can_parse(
    store: InMemoryDesignStore,
) -> None:
    """A `JSON.parse` failure in `ResultBlock` renders nothing at all, not an error."""

    async def _body() -> None:
        payload = await tools.structure_experiment_request(_request())
        parsed = json.loads(payload)
        assert parsed["design_id"].startswith("design-")
        assert ProtocolReceipt.model_validate(parsed).model_dump_json() == payload

    asyncio.run(_body())


def test_structuring_the_same_ask_twice_revises_rather_than_forking(
    store: InMemoryDesignStore,
) -> None:
    """The id is derived from the ask, which is what stops a re-reading opening a second design.

    **And an identical re-reading writes nothing**, which this test used to assert the opposite of.
    `advanced()` retires an `approved` or `executed` status on any revision landing, justified by
    "the document has changed" — so a second revision carrying a document that compares equal to
    the first un-approved a plate nobody had touched. Measured: a chemist approved a design, the
    ask was restated, and the header came back `draft` over a head identical to the approved one.
    The design is still reached (same id, same head); there is simply nothing to record.
    """

    async def _body() -> None:
        first = await _open()
        second = await _open()
        assert second.design_id == first.design_id
        assert (first.revision, second.revision) == (1, 1)
        assert len(await store.history(first.design_id)) == 1

    asyncio.run(_body())


def test_a_salt_is_how_a_second_design_for_one_ask_is_opened(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        first = await _open()
        forked = ProtocolReceipt.model_validate_json(
            await tools.structure_experiment_request(_request(), salt="second")
        )
        assert forked.design_id != first.design_id
        assert forked.revision == 1

    asyncio.run(_body())


def test_structuring_refuses_before_it_stores_anything(
    store: InMemoryDesignStore,
) -> None:
    """The quote check runs first, so a refused intake leaves no half-written design behind."""

    async def _body() -> None:
        request = _request(scale=RequestField(value="5 g", basis="stated", quote="five grams"))
        with pytest.raises(ChemclawError, match="quote is not in the message"):
            await tools.structure_experiment_request(request)
        assert await store.read(design_id_for(request, owner=require_actor())) is None

    asyncio.run(_body())


# --- draft_experiment_protocol ------------------------------------------------------------------


def test_drafting_against_an_unknown_design_names_the_tool_that_opens_one(
    store: InMemoryDesignStore,
) -> None:
    """The intake is a prerequisite, so its absence has to be a refusal that says so.

    The tool composes the design out of the *stored* request, so there is nothing it could draft
    against an id nobody opened — and a model that skipped the intake needs to be told which call
    it skipped rather than that a lookup returned nothing.
    """

    async def _body() -> None:
        with pytest.raises(ChemclawError, match="structure_experiment_request") as refusal:
            await _draft("design-nothing", 0)
        assert "design-nothing" in str(refusal.value)

    asyncio.run(_body())


def test_drafting_refuses_a_design_that_cites_nothing(
    store: InMemoryDesignStore,
) -> None:
    """The blocker that makes "use the record and the tools" a property of the code."""

    async def _body() -> None:
        opened = await _open()
        with pytest.raises(ChemclawError, match="evidence_present"):
            await _draft(opened.design_id, opened.revision, cited=False)

        # The intake is still the whole history: a refused draft leaves no revision behind.
        assert [item.kind for item in await store.history(opened.design_id)] == ["request"]

    asyncio.run(_body())


def test_drafting_accepts_a_design_that_cites_a_precedent_and_a_tool(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        opened = await _open()
        payload = await _draft(opened.design_id, opened.revision)
        receipt = ProtocolReceipt.model_validate_json(payload)
        # A `ProtocolReceipt` and nothing else: what the browser parses is what the model reads.
        assert json.loads(payload)["design_id"] == receipt.design_id
        assert receipt.model_dump_json() == payload
        assert receipt.design_id == opened.design_id
        assert receipt.revision == 2
        assert receipt.status == "draft"
        assert receipt.blocking == []
        assert receipt.evidence_count == 2
        assert receipt.arm_count == 1

        stored = await store.read(receipt.design_id)
        assert stored is not None
        assert stored.kind == "protocol" and stored.author_kind == "agent"
        # The whole composed design, request included — which the call never passed.
        assert stored.design == _protocol()

    asyncio.run(_body())


def test_the_ask_a_draft_is_checked_against_is_the_stored_one(
    store: InMemoryDesignStore,
) -> None:
    """The tool takes no request, so the one it enforces can only have come from the store.

    Proven through a limit rather than through an echo: the intake forbids DMF, the draft charges
    it, and `forbidden_absent` refuses — a design composed from anything but the stored ask would
    have no exclusions to break.
    """

    async def _body() -> None:
        opened = await _open(_request(forbidden=["DMF"]))
        with pytest.raises(ChemclawError, match="forbidden_absent") as refusal:
            await _draft(
                opened.design_id,
                opened.revision,
                base=ProtocolBody(
                    charge=[
                        ChargeLine(component="aryl chloride", limiting=True, amount_mmol=1.0),
                        ChargeLine(component="DMF", volume_ml=2.0),
                    ]
                ),
            )
        assert "DMF" in str(refusal.value)

    asyncio.run(_body())


def test_a_draft_without_a_change_note_is_refused(
    store: InMemoryDesignStore,
) -> None:
    """The one field that makes the revision history readable a year later.

    Required on the *first* draft as much as on a revision of it: one tool creates and revises
    alike, so there is no call the note is optional on — and a blank one is a blank one whether it
    is empty or whitespace.
    """

    async def _body() -> None:
        opened = await _open()
        with pytest.raises(ChemclawError, match="change_note"):
            await _draft(opened.design_id, opened.revision, change_note="")
        with pytest.raises(ChemclawError, match="change_note"):
            await _draft(opened.design_id, opened.revision, change_note="   ")
        assert [item.revision for item in await store.history(opened.design_id)] == [1]

        await _draft(opened.design_id, opened.revision)
        with pytest.raises(ChemclawError, match="change_note"):
            await _draft(opened.design_id, 2, arms=2, change_note="")
        assert [item.revision for item in await store.history(opened.design_id)] == [1, 2]

    asyncio.run(_body())


def test_a_revision_reports_the_paths_it_changed(
    store: InMemoryDesignStore,
) -> None:
    """The signal the revision table exists for: what the next author actually moved."""

    async def _body() -> None:
        opened = await _open()
        await _draft(opened.design_id, opened.revision)
        second = ProtocolReceipt.model_validate_json(
            await _draft(opened.design_id, 2, arms=2, change_note="added the second arm")
        )
        assert second.revision == 3
        assert "arms.A2.arm_id" in second.changed_paths

    asyncio.run(_body())


def test_a_revision_derived_from_a_stale_head_is_refused(
    store: InMemoryDesignStore,
) -> None:
    """The store's `RevisionConflict` reaches the model as a message telling it to re-read."""

    async def _body() -> None:
        opened = await _open()
        await _draft(opened.design_id, opened.revision)
        await _draft(opened.design_id, 2, arms=2, change_note="added the second arm")
        with pytest.raises(ChemclawError, match="is at revision 3"):
            await _draft(
                opened.design_id, 2, arms=3, change_note="added a third arm from a stale read"
            )

    asyncio.run(_body())


def test_a_plate_format_computes_the_layout(store: InMemoryDesignStore) -> None:
    """The arms come in as a list and go out as a plate, laid out row-major."""

    async def _body() -> None:
        opened = await _open()
        receipt = ProtocolReceipt.model_validate_json(
            await _draft(opened.design_id, opened.revision, arms=4, plate_format=24)
        )
        assert receipt.plate_format == 24

        stored = await store.read(receipt.design_id)
        assert stored is not None and stored.design.layout is not None
        assert [well.label for well in stored.design.layout.wells] == ["A1", "A2", "A3", "A4"]
        assert [row.well for row in receipt.arms] == ["A1", "A2", "A3", "A4"]

    asyncio.run(_body())


def test_a_randomized_plate_records_the_seed_that_reproduces_it(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        opened = await _open()
        receipt = ProtocolReceipt.model_validate_json(
            await _draft(
                opened.design_id,
                opened.revision,
                arms=6,
                plate_format=24,
                randomize_run_order=True,
                seed=5,
            )
        )
        stored = await store.read(receipt.design_id)
        assert stored is not None and stored.design.layout is not None
        assert stored.design.layout.randomized is True and stored.design.layout.seed == 5
        assert sorted(well.run_order for well in stored.design.layout.wells) == [1, 2, 3, 4, 5, 6]

    asyncio.run(_body())


def test_a_plate_that_cannot_hold_the_arms_names_the_smallest_that_can(
    store: InMemoryDesignStore,
) -> None:
    """A refusal that tells the model what to pass instead is one it can act on in one turn."""

    async def _body() -> None:
        opened = await _open()
        with pytest.raises(ChemclawError, match=r"smallest plate that holds 30 arms is 48"):
            await _draft(opened.design_id, opened.revision, arms=30, plate_format=24)
        assert [item.kind for item in await store.history(opened.design_id)] == ["request"]

    asyncio.run(_body())


def test_an_unknown_plate_format_is_refused_before_anything_is_stored(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        opened = await _open()
        with pytest.raises(ChemclawError, match="unknown plate format 60"):
            await _draft(opened.design_id, opened.revision, arms=4, plate_format=60)
        assert [item.kind for item in await store.history(opened.design_id)] == ["request"]

    asyncio.run(_body())


def test_drafting_after_structuring_the_same_ask_stores_the_next_revision(
    store: InMemoryDesignStore,
) -> None:
    """The documented workflow, which is now the only one: structure the ask, then draft for it.

    `structure_experiment_request` files the ask under the actor's own `design_id_for` and hands
    back the id and the revision the draft builds on — so the protocol is revision 2 of the design
    the intake opened, and the two revisions are one document growing rather than two designs.
    """

    async def _body() -> None:
        request = _request()
        intake = await _open(request)
        assert intake.design_id == design_id_for(request, owner=require_actor())

        drafted = ProtocolReceipt.model_validate_json(
            await _draft(intake.design_id, intake.revision)
        )
        assert drafted.design_id == intake.design_id
        assert drafted.revision == 2
        assert drafted.status == "draft"

        history = await store.history(intake.design_id)
        assert [item.kind for item in history] == ["request", "protocol"]
        assert [item.parent_revision for item in history] == [0, 1]
        assert history[-1].design.request == request

    asyncio.run(_body())


#: One factor and its two levels, so a screen's arms have something to set. `factor_levels_declared`
#: is a blocker, so an arm that sets nothing a factor declares would refuse the draft before the
#: property under test could be reached.
_LIGAND = Factor(
    name="ligand",
    kind="categorical",
    levels=[FactorLevel(label="XPhos"), FactorLevel(label="SPhos")],
)


def _screen_arms() -> list[ProtocolArm]:
    """One arm per level of `_LIGAND`, each setting it — the least a screen can be."""
    return [
        ProtocolArm(arm_id="A1", levels={"ligand": "XPhos"}),
        ProtocolArm(arm_id="A2", levels={"ligand": "SPhos"}),
    ]


def test_restructuring_the_ask_keeps_the_protocol_it_was_drafted_into(
    store: InMemoryDesignStore,
) -> None:
    """Correcting the ask revises the *ask*; it does not throw the plate away.

    The id is derived from the ask, so re-structuring the same one reaches the same design — and
    this tool used to append a bare `ExperimentDesign(request=…)` over it. Measured: `arm_count`
    reset to 0, the factors and the layout vanished, and every default read (the listing, `GET
    /protocols/{id}`, `read_experiment_protocol`) served the empty ask, because no consumer reads a
    non-head revision. The correction has to land and the procedure has to survive it.
    """

    async def _body() -> None:
        opened = await _open()
        await _draft(
            opened.design_id,
            opened.revision,
            arms=_screen_arms(),
            factors=[_LIGAND],
            plate_format=24,
        )

        # A *corrected* ask, not a different one: `design_id_for` reads title, goal, reaction and
        # mode, so correcting any of those would open a second design rather than revise this one.
        corrected = _request(
            scale=RequestField(value="100 mg", basis="inferred"),
            notes="they meant 100 mg, not the 5 g I first read",
        )
        again = ProtocolReceipt.model_validate_json(
            await tools.structure_experiment_request(corrected)
        )
        assert again.design_id == opened.design_id
        assert again.revision == 3

        head = await store.read(opened.design_id)
        assert head is not None
        assert head.kind == "request"
        # The correction landed…
        assert head.design.request == corrected
        # …and the protocol it was drafted into is still there, whole.
        assert [arm.arm_id for arm in head.design.arms] == ["A1", "A2"]
        assert [factor.name for factor in head.design.factors] == ["ligand"]
        assert head.design.layout is not None
        assert head.design.layout.plate_format == 24
        assert [well.label for well in head.design.layout.wells] == ["A1", "A2"]
        assert head.design.evidence == _cited()
        # The receipt says the same thing, which is what the model reads back.
        assert again.arm_count == 2
        assert again.status == "draft"

    asyncio.run(_body())


def test_restructuring_grades_the_checks_at_the_stage_the_design_is_at(
    store: InMemoryDesignStore,
) -> None:
    """A design holding a protocol is graded as a protocol, even by the intake tool.

    The request stage reports every protocol-only check as a passing `note` reading "not checked yet
    — this design holds only the ask". That is right for an intake and wrong the moment the design
    has a procedure: a protocol that now contradicts a corrected ask is exactly what a chemist needs
    to see, and grading it at the request stage would report `is_a_protocol` and `evidence_present`
    as unexamined on a design that has both.
    """

    async def _body() -> None:
        first = await _open()
        intake = {check.check_id: check for check in first.checks}
        # The intake's own grading, for contrast: this is what the design under test must *not* get.
        assert intake["is_a_protocol"].severity == "note"
        assert "not checked yet" in intake["is_a_protocol"].detail

        await _draft(first.design_id, first.revision, arms=2)
        again = ProtocolReceipt.model_validate_json(
            await tools.structure_experiment_request(_request(notes="corrected"))
        )

        graded = {check.check_id: check for check in again.checks}
        assert graded["is_a_protocol"].severity == "blocker"
        assert graded["is_a_protocol"].passed is True
        assert "2 arm(s)" in graded["is_a_protocol"].detail
        assert graded["evidence_present"].severity == "blocker"
        assert graded["evidence_present"].passed is True
        assert "not checked yet" not in graded["evidence_present"].detail
        assert again.blocking == []

    asyncio.run(_body())


def test_a_revision_that_passes_no_plate_format_carries_the_plate_forward(
    store: InMemoryDesignStore,
) -> None:
    """A revision that only changes a temperature must not delete the plate.

    `plate_format` defaults to 0, and 0 used to mean "no layout" rather than "do not re-lay it out"
    — so the well assignments and the run order were silently dropped, and `layout_fits` degraded to
    a *passing* warning reading "no plate layout", which is why nothing said so. A randomised order
    is not recoverable either: a fresh `place()` with another seed is a different plate, and the one
    a chemist ran is the one the seed reproduces.
    """

    async def _body() -> None:
        opened = await _open()
        await _draft(
            opened.design_id,
            opened.revision,
            arms=6,
            plate_format=24,
            randomize_run_order=True,
            seed=5,
        )
        drafted = await store.read(opened.design_id)
        assert drafted is not None and drafted.design.layout is not None
        before = drafted.design.layout

        revised = ProtocolReceipt.model_validate_json(
            await _draft(
                opened.design_id,
                2,
                arms=6,
                base=ProtocolBody(setpoints=Setpoints(temperature_c=60.0)),
                change_note="60 C, the chloride decomposes at 80",
            )
        )
        assert revised.revision == 3
        assert revised.plate_format == 24

        head = await store.read(opened.design_id)
        assert head is not None and head.design.layout is not None
        # Byte-identical: the same wells, the same run order, the same seed.
        assert head.design.layout.model_dump_json() == before.model_dump_json()
        assert head.design.layout.randomized is True and head.design.layout.seed == 5
        assert head.design.base.setpoints.temperature_c == 60.0

        # And the check that used to go quiet about it is still grading a real plate.
        layout_check = {check.check_id: check for check in head.checks}["layout_fits"]
        assert "no plate layout" not in layout_check.detail

    asyncio.run(_body())


def test_passing_a_plate_format_on_a_revision_lays_the_plate_out_again(
    store: InMemoryDesignStore,
) -> None:
    """Carrying the plate forward is what *omitting* the format asks for; passing one re-lays it.

    The other half of the same rule, and the reason the first is not simply "layouts are immutable":
    moving a screen onto a bigger plate, or dropping the randomisation, is a `plate_format` away.
    """

    async def _body() -> None:
        opened = await _open()
        await _draft(
            opened.design_id,
            opened.revision,
            arms=6,
            plate_format=24,
            randomize_run_order=True,
            seed=5,
        )

        revised = ProtocolReceipt.model_validate_json(
            await _draft(
                opened.design_id,
                2,
                arms=6,
                plate_format=48,
                change_note="moved it onto a 48-well plate and dropped the randomisation",
            )
        )
        assert revised.plate_format == 48

        head = await store.read(opened.design_id)
        assert head is not None and head.design.layout is not None
        assert head.design.layout.plate_format == 48
        assert head.design.layout.randomized is False and head.design.layout.seed is None
        assert [well.run_order for well in head.design.layout.wells] == [1, 2, 3, 4, 5, 6]

    asyncio.run(_body())


def test_drafting_refuses_citations_that_name_nothing_to_open(
    store: InMemoryDesignStore,
) -> None:
    """Two sentences about work nobody can check are not two citations.

    `kind="tool"` with no `tool` name and `kind="precedent"` with no `ref` cleared
    `evidence_present` between them, which made the one blocker in this tier satisfiable by writing
    prose. Asserted through the tool rather than through `checks` alone, because that is the path a
    model takes and the path the refusal has to reach it on.
    """

    async def _body() -> None:
        opened = await _open()
        with pytest.raises(ChemclawError, match="evidence_present"):
            await _draft(
                opened.design_id,
                opened.revision,
                evidence=[
                    EvidenceRef(kind="precedent", summary="a run like this gave 72%"),
                    EvidenceRef(kind="tool", summary="the base is strong enough"),
                ],
            )
        assert [item.kind for item in await store.history(opened.design_id)] == ["request"]

    asyncio.run(_body())


# --- read_experiment_protocol -------------------------------------------------------------------


def test_reading_an_unknown_design_is_refused_with_a_next_step(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        with pytest.raises(ChemclawError, match="find_experiment_protocols"):
            await tools.read_experiment_protocol("design-nothing")

    asyncio.run(_body())


def test_reading_an_unknown_revision_names_the_revision(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        opened = await _open()
        await _draft(opened.design_id, opened.revision)
        with pytest.raises(ChemclawError, match="at revision 9"):
            await tools.read_experiment_protocol(opened.design_id, revision=9)

    asyncio.run(_body())


def test_a_read_returns_the_receipt_the_document_and_the_prose(
    store: InMemoryDesignStore,
) -> None:
    """Three forms of one design.

    What a model quotes, what the browser renders, and what a report carries — rebuilding the third
    from the second in a turn would render it differently every time.
    """

    async def _body() -> None:
        opened = await _open()
        await _draft(opened.design_id, opened.revision, arms=2)

        payload = await tools.read_experiment_protocol(opened.design_id)
        readout = ProtocolReadout.model_validate_json(payload)
        assert readout.receipt.design_id == opened.design_id
        assert readout.receipt.revision == 2
        assert readout.design == _protocol(arms=2)
        assert readout.markdown.startswith("# SM-3 Suzuki")
        assert "## Evidence" in readout.markdown
        assert readout.model_dump_json() == payload

    asyncio.run(_body())


def test_a_read_selects_the_revision_it_is_asked_for(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        opened = await _open()
        await _draft(opened.design_id, opened.revision)
        await _draft(opened.design_id, 2, arms=3, change_note="widened the screen")

        head = ProtocolReadout.model_validate_json(
            await tools.read_experiment_protocol(opened.design_id)
        )
        earlier = ProtocolReadout.model_validate_json(
            await tools.read_experiment_protocol(opened.design_id, revision=2)
        )
        assert (head.receipt.revision, len(head.design.arms)) == (3, 3)
        assert (earlier.receipt.revision, len(earlier.design.arms)) == (2, 1)

    asyncio.run(_body())


# --- find_experiment_protocols ------------------------------------------------------------------


def test_finding_rejects_a_status_that_is_not_a_status(
    store: InMemoryDesignStore,
) -> None:
    """A silently-empty listing would read as "there are none" rather than "you asked wrongly"."""

    async def _body() -> None:
        with pytest.raises(ChemclawError, match="unknown status 'in-progress'"):
            await tools.find_experiment_protocols(status="in-progress")

    asyncio.run(_body())


def test_finding_lists_what_was_stored_and_filters_by_status(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        await _open(_request(title="the ask"))
        drafted = await _open(_request(title="the draft"))
        await _draft(drafted.design_id, drafted.revision)

        everything = DesignListing.model_validate_json(await tools.find_experiment_protocols())
        assert {summary.title for summary in everything.designs} == {"the ask", "the draft"}

        drafts = DesignListing.model_validate_json(
            await tools.find_experiment_protocols(status="draft")
        )
        assert [summary.title for summary in drafts.designs] == ["the draft"]
        # Two revisions, because a draft is always the intake's next one.
        assert drafts.designs[0].head_revision == 2

    asyncio.run(_body())


def test_finding_filters_by_project_and_returns_parseable_json(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        for title, project in (("in prj-a", "prj-a"), ("in prj-b", "prj-b")):
            opened = await _open(_request(title=title, project=project))
            await _draft(opened.design_id, opened.revision)

        payload = await tools.find_experiment_protocols(project="prj-a")
        assert json.loads(payload)["designs"][0]["project"] == "prj-a"
        listing = DesignListing.model_validate_json(payload)
        assert [summary.title for summary in listing.designs] == ["in prj-a"]
        assert listing.model_dump_json() == payload

    asyncio.run(_body())


def test_an_empty_listing_is_valid_json_rather_than_nothing(
    store: InMemoryDesignStore,
) -> None:
    async def _body() -> None:
        payload = await tools.find_experiment_protocols()
        assert json.loads(payload) == {"designs": []}

    asyncio.run(_body())


def test_the_tool_does_not_let_its_caller_supply_the_words_it_grades_against() -> None:
    """The parameter's absence is the control, so its absence is what a test has to pin.

    `source_text` used to be an argument. A model that wanted `basis="stated"` supplied one
    containing its own quotes and got it — measured: the same request refused against the real user
    text and accepted against an invented one. The fix is not a better comparison, it is that the
    caller cannot reach the haystack at all.
    """
    parameters = set(inspect.signature(tools.structure_experiment_request).parameters)
    assert parameters == {"request", "salt"}
    assert "source_text" not in parameters


def test_a_stated_slot_is_refused_when_no_chemist_spoke(store: InMemoryDesignStore) -> None:
    """`require_actor`'s reject-if-absent rule, applied to the words instead of the person.

    Off a turn there is nobody to have said it, so `stated` is refused rather than waived — a check
    that passed when its evidence was missing would be one a caller can switch off by calling from
    somewhere else.
    """

    async def _body() -> None:
        set_current_user_text(None)  # the autouse fixture resets it after the test
        stated = _request(scale=RequestField(value="2 g", basis="stated", quote="24 wells, no DMF"))
        with pytest.raises(ChemclawError, match="no chemist message"):
            await tools.structure_experiment_request(stated)
        assert await store.listing() == []

    asyncio.run(_body())


def test_an_inferred_ask_needs_no_chemist_message(store: InMemoryDesignStore) -> None:
    """The other direction, which is what stops the refusal above from being a blanket one."""

    async def _body() -> None:
        set_current_user_text(None)  # the autouse fixture resets it after the test
        inferred = _request(scale=RequestField(value="2 g", basis="inferred"))
        payload = ProtocolReceipt.model_validate_json(
            await tools.structure_experiment_request(inferred)
        )
        assert payload.revision == 1

    asyncio.run(_body())


def test_a_stated_quote_has_to_say_the_value_it_is_offered_for() -> None:
    """`stated` attests a *value*, and only the *quote* was ever checked.

    Both halves passed as long as the quote occurred somewhere in the message, and any substring
    occurs somewhere. Measured against a chemist who wrote "We need to get the Suzuki on the
    deactivated chloride working. Try what you think.", a model stored four limits the chemist never
    named as their own words: `scale='5 g'` quoting `'working'`, `plate_format='96'` quoting
    `'the'`, `max_runs='96'` quoting `'Suzuki'` and `deadline='2026-09-01'` quoting `'.'`.
    """
    said = "We need to get the Suzuki on the deactivated chloride working. Try what you think."
    token = set_current_user_text(said)
    try:
        for value, quote in (("5 g", "working"), ("96", "the"), ("2026-09-01", ".")):
            request = _request(scale=RequestField(value=value, basis="stated", quote=quote))
            with pytest.raises(ChemclawError, match="does not say their value"):
                tools.require_quotes_are_verbatim(request, get_current_user_text())
    finally:
        reset_current_user_text(token)


def test_an_honest_quote_still_passes_including_a_normalised_one() -> None:
    """The half this must not cost: a real constraint pushed into `inferred` is the same defect."""
    said = "Run it on 250 mg of the bromide, 24 wells, and I need it by Friday."
    token = set_current_user_text(said)
    try:
        for value, quote in (
            ("250 mg", "250 mg of the bromide"),
            ("24", "24 wells"),
            # The chemist stated a deadline and the model normalised it; the quote carries no
            # figures, so the value's are not required to appear in it.
            ("2026-09-01", "by Friday"),
        ):
            request = _request(scale=RequestField(value=value, basis="stated", quote=quote))
            tools.require_quotes_are_verbatim(request, get_current_user_text())
    finally:
        reset_current_user_text(token)


def test_a_quote_stating_a_different_figure_is_refused() -> None:
    """A quote reading 'no more than 48 runs' cannot be evidence for `max_runs='96'`."""
    said = "Keep it to no more than 48 runs please."
    token = set_current_user_text(said)
    try:
        request = _request(
            max_runs=RequestField(value="96", basis="stated", quote="no more than 48 runs")
        )
        with pytest.raises(ChemclawError, match="does not say their value"):
            tools.require_quotes_are_verbatim(request, get_current_user_text())
    finally:
        reset_current_user_text(token)
