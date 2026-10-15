"""No route returns or changes another tenant's rows.

Every route the front door serves has a tenant verdict here: it is probed (`_probes`), or it is
neutral for a stated reason (`_NEUTRAL`). A new route with neither fails the verdict test below.

The probe test runs the production app against a real database as a role the policies bind, with
two tenants and **one person in both**, so nothing but the tenant separates what that person may
read in one from what they may read in the other. ACME's data is seeded with a marker. Every probe
is made by ACME first, to prove the request reaches something, and by GLOBEX second, naming ACME's
own identifiers. Afterwards no GLOBEX response may contain the marker, ACME's rows are byte for byte
what they were, and the destructive probes are repeated by ACME to show their targets were real.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore
from temporalio.service import RPCError, RPCStatusCode

from chemclaw.agent.behaviour_proposals import PostgresProposalStore, Proposal, content_hash
from chemclaw.agent.session_events import record_session_event
from chemclaw.agent.session_store import PostgresHistoryProvider, SessionOwnerStore
from chemclaw.api.app import create_app
from chemclaw.api.tool_results import store_tool_result
from chemclaw.durable import pending_store
from chemclaw.durable.check_in import CHECK_IN_KIND
from chemclaw.durable.digest import DIGEST_KIND, digest_channel
from chemclaw.durable.job_record import JobRecord
from chemclaw.durable.job_record_store import PostgresJobRecordSink
from chemclaw.durable.template_job import template_fingerprint
from chemclaw.protocols.checks import run_checks
from chemclaw.protocols.store import PostgresDesignStore
from chemclaw.science.calc.postgres_artifacts import PostgresArtifactStore
from chemclaw.skills.store import save_local_skill, save_org_skill
from chemclaw.templates.composed import ComposedWorkflow, PostgresComposedStore
from tests.tenancy_app import (
    ACME,
    GLOBEX,
    MARKER,
    OTHER,
    PERSON,
    REVIEWER,
    World,
    _no_connectors,
    two_tenant_world,
)
from tests.test_composed_workflows import _READ_STEP, _document
from tests.test_protocol_routes import _design

Route = tuple[str, str]

# Routes that read no tenant's data, each with why. A route is here or it is probed.
_NEUTRAL: dict[Route, str] = {
    ("GET", "/healthz"): "liveness: answers whether the process is up, reads nothing",
    ("GET", "/readyz"): "readiness: the migration ledger, which is global, and the vectors' width",
    ("GET", "/metrics"): "process counters; the exposition carries no tenant or user content",
    ("GET", "/openapi.json"): "the contract document: the same for everyone",
    ("GET", "/profiles"): "the registered agent profiles: deployment configuration, not data",
    ("GET", "/notes/{note_id}"): (
        "the knowledge graph is one repository for the deployment (`kg_store=git`); the start-up "
        "guard refuses a second tenant until it is held in Postgres, where `kg_notes` is scoped"
    ),
}


@dataclass
class Ids:
    """The identifiers the probes name: ACME's, which GLOBEX tries to reach."""

    session: str = "S-ACME"
    second: str = "S-ACME-2"
    exhibit: str = "X-ACME"
    ref: str = "R-ACME"
    ticket: str = "1"
    job: str = "acme~job"
    request: str = "acme~await"
    proposal: str = "acme-proposal"
    proposal_hash: str = "h"
    design: str = "design-acme"
    workflow: str = "acme-workflow"
    fingerprint: str = "f"
    skill: str = "acme-skill"
    org_skill: str = "acme-house-skill"
    org_hash: str = "h"
    calc_ref: str = "acme-key#geometry.xyz"


@dataclass(frozen=True)
class Probe:
    """One request, made by ACME to show it reaches something and by GLOBEX to show it does not.

    `own` is the status ACME gets (`None` when the request is a stream or cannot succeed in a
    test); `foreign` is every status GLOBEX may get. `last` repeats ACME's request at the end,
    because it removes or consumes its own target. `own_contains` is text ACME's answer must hold,
    and `foreign_excludes` text GLOBEX's successful answer must not.
    """

    route: Route
    path: str
    own: int | None
    foreign: tuple[int, ...] = (404,)
    kwargs: dict[str, Any] = field(default_factory=dict)
    oid: str = PERSON
    roles: tuple[str, ...] = ()
    last: bool = False
    own_contains: str = ""
    foreign_excludes: str = ""


def _skill(name: str, note: str) -> str:
    """A well-formed `SKILL.md` carrying `note`."""
    return f"---\nname: {name}\ndescription: how this house works\n---\n\n{note}\n"


def _probes(ids: Ids) -> list[Probe]:
    """Every probe, for the identifiers `ids` names."""
    s, x = ids.session, ids.exhibit
    table = {"kind": "table", "columns": [{"key": "a", "label": "A"}], "rows": [{"a": MARKER}]}
    reviewer = (REVIEWER,)
    return [
        # --- sessions ------------------------------------------------------------------------
        Probe(("GET", "/sessions"), "/sessions", 200, (200,), own_contains=s, foreign_excludes=s),
        Probe(("POST", "/sessions"), "/sessions", 200, (200,)),
        Probe(
            ("GET", "/sessions/shared"),
            "/sessions/shared",
            200,
            (200,),
            oid=OTHER,
            own_contains=s,
            foreign_excludes=s,
        ),
        Probe(
            ("GET", "/sessions/{session_id}/messages"),
            f"/sessions/{s}/messages",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("POST", "/sessions/{session_id}/messages"),
            f"/sessions/{s}/messages",
            None,
            kwargs={"json": {"message": "what did we find?"}},
        ),
        Probe(("GET", "/sessions/{session_id}/events"), f"/sessions/{s}/events", None),
        Probe(("GET", "/sessions/{session_id}/plan"), f"/sessions/{s}/plan", 200),
        Probe(
            ("POST", "/sessions/{session_id}/plan/decision"),
            f"/sessions/{s}/plan/decision",
            None,
            kwargs={"json": {"plan_hash": "x", "approved": True}},
        ),
        Probe(("GET", "/sessions/{session_id}/queue"), f"/sessions/{s}/queue", 200),
        Probe(
            ("DELETE", "/sessions/{session_id}/queue/{ticket}"),
            f"/sessions/{s}/queue/{ids.ticket}",
            None,
        ),
        Probe(("POST", "/sessions/{session_id}/turn/stop"), f"/sessions/{s}/turn/stop", None),
        Probe(("GET", "/sessions/{session_id}/turn/stream"), f"/sessions/{s}/turn/stream", None),
        Probe(
            ("GET", "/sessions/{session_id}/tool-results/{ref}"),
            f"/sessions/{s}/tool-results/{ids.ref}",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("POST", "/sessions/{session_id}/attachments"),
            f"/sessions/{s}/attachments",
            200,
            kwargs={"files": {"file": ("notes.csv", f"a,b\n{MARKER},2\n".encode(), "text/csv")}},
        ),
        # A session that has taken no turn has no saved state to copy: 409 is the route answering.
        Probe(("POST", "/sessions/{session_id}/fork"), f"/sessions/{s}/fork", 409),
        Probe(("DELETE", "/sessions/{session_id}"), f"/sessions/{ids.second}", 204, last=True),
        Probe(("GET", "/plans/pending"), "/plans/pending", 200, (200,)),
        # --- members -------------------------------------------------------------------------
        Probe(
            ("GET", "/sessions/{session_id}/members"),
            f"/sessions/{s}/members",
            200,
            own_contains=OTHER,
        ),
        Probe(
            ("PUT", "/sessions/{session_id}/members/{actor}"),
            f"/sessions/{s}/members/{OTHER}",
            204,
        ),
        Probe(
            ("DELETE", "/sessions/{session_id}/members/{actor}"),
            f"/sessions/{s}/members/{OTHER}",
            204,
            last=True,
        ),
        # --- artefacts -----------------------------------------------------------------------
        Probe(("GET", "/exhibits"), "/exhibits", 200, (200,), own_contains=x, foreign_excludes=x),
        Probe(
            ("GET", "/sessions/{session_id}/exhibits"),
            f"/sessions/{s}/exhibits",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("POST", "/sessions/{session_id}/exhibits"),
            f"/sessions/{s}/exhibits",
            201,
            kwargs={"json": {"kind": "table", "title": "t", "spec": table}},
        ),
        Probe(
            ("GET", "/sessions/{session_id}/exhibits/{exhibit_id}"),
            f"/sessions/{s}/exhibits/{x}",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("GET", "/sessions/{session_id}/exhibits/{exhibit_id}/diff"),
            f"/sessions/{s}/exhibits/{x}/diff",
            200,
        ),
        Probe(
            ("GET", "/sessions/{session_id}/exhibits/{exhibit_id}/export.{fmt}"),
            f"/sessions/{s}/exhibits/{x}/export.csv",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("GET", "/sessions/{session_id}/exhibits/{exhibit_id}/revisions"),
            f"/sessions/{s}/exhibits/{x}/revisions",
            200,
            own_contains=PERSON,
        ),
        Probe(
            ("POST", "/sessions/{session_id}/exhibits/{exhibit_id}/revisions"),
            f"/sessions/{s}/exhibits/{x}/revisions",
            201,
            kwargs={"json": {"parent_revision": 1, "spec": table}},
        ),
        # --- jobs, waits and mailboxes -------------------------------------------------------
        Probe(
            ("GET", "/jobs"), "/jobs", 200, (200,), own_contains=ids.job, foreign_excludes=ids.job
        ),
        Probe(("GET", "/jobs/{job_id}"), f"/jobs/{ids.job}", 200, own_contains=ids.job),
        Probe(("DELETE", "/jobs/{job_id}"), f"/jobs/{ids.job}", 202, roles=reviewer),
        Probe(
            ("GET", "/pending"),
            "/pending",
            200,
            (200,),
            own_contains=ids.request,
            foreign_excludes=ids.request,
        ),
        Probe(
            ("POST", "/pending/{request_id}/answer"),
            f"/pending/{ids.request}/answer",
            204,
            kwargs={"json": {"payload": {"yield": 61}}},
        ),
        Probe(
            ("GET", "/digests"),
            "/digests",
            200,
            (200,),
            last=True,
            own_contains=MARKER,
            foreign_excludes=MARKER,
        ),
        Probe(
            ("GET", "/check-ins"),
            "/check-ins",
            200,
            (200,),
            last=True,
            own_contains=MARKER,
            foreign_excludes=MARKER,
        ),
        Probe(
            ("GET", "/calc-artifacts/content"),
            "/calc-artifacts/content",
            200,
            kwargs={"params": {"ref": ids.calc_ref}},
            own_contains=MARKER,
        ),
        Probe(
            ("GET", "/schedules"),
            "/schedules",
            200,
            (200,),
            own_contains="orphaned-waits@acme",
            foreign_excludes="acme",
        ),
        # --- proposals and skills ------------------------------------------------------------
        Probe(
            ("GET", "/proposals"),
            "/proposals",
            200,
            (200,),
            own_contains=ids.proposal,
            foreign_excludes=ids.proposal,
        ),
        Probe(
            ("POST", "/proposals/{kind}/{name}"),
            f"/proposals/skill/{ids.proposal}",
            200,
            kwargs={"json": {"content_hash": ids.proposal_hash, "accepted": False}},
            last=True,
        ),
        Probe(
            ("GET", "/skills/mine"),
            "/skills/mine",
            200,
            (200,),
            own_contains=ids.skill,
            foreign_excludes=ids.skill,
        ),
        Probe(
            ("GET", "/skills/mine/{name}"),
            f"/skills/mine/{ids.skill}",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("POST", "/skills/mine"),
            "/skills/mine",
            200,
            (200,),
            kwargs={"json": {"body": _skill("scratch-skill", "x")}},
        ),
        Probe(("DELETE", "/skills/mine/{name}"), f"/skills/mine/{ids.skill}", 200, last=True),
        Probe(
            ("GET", "/skills/org"),
            "/skills/org",
            200,
            (200,),
            own_contains=ids.org_skill,
            foreign_excludes=ids.org_skill,
        ),
        Probe(
            ("GET", "/skills/org/{name}"),
            f"/skills/org/{ids.org_skill}",
            200,
            own_contains=MARKER,
        ),
        Probe(
            ("GET", "/skills/org/{name}/versions"),
            f"/skills/org/{ids.org_skill}/versions",
            200,
            (200, 404),
            foreign_excludes=MARKER,
        ),
        Probe(
            ("POST", "/skills/org"),
            "/skills/org",
            200,
            (200,),
            roles=reviewer,
            kwargs={"json": {"body": _skill("scratch-house", "x")}},
        ),
        Probe(
            ("POST", "/skills/org/{name}/revert"),
            f"/skills/org/{ids.org_skill}/revert",
            200,
            roles=reviewer,
            kwargs={"json": {"content_hash": ids.org_hash}},
        ),
        Probe(
            ("DELETE", "/skills/org/{name}"),
            f"/skills/org/{ids.org_skill}",
            200,
            roles=reviewer,
            last=True,
        ),
        # --- designs and composed workflows --------------------------------------------------
        Probe(
            ("GET", "/protocols"),
            "/protocols",
            200,
            (200,),
            own_contains=ids.design,
            foreign_excludes=ids.design,
        ),
        Probe(
            ("GET", "/protocols/{design_id}"),
            f"/protocols/{ids.design}",
            200,
            own_contains=ids.design,
        ),
        Probe(
            ("GET", "/protocols/{design_id}/diff"),
            f"/protocols/{ids.design}/diff",
            200,
            kwargs={"params": {"from_revision": 1}},
        ),
        Probe(
            ("GET", "/protocols/{design_id}/run-sheet.csv"),
            f"/protocols/{ids.design}/run-sheet.csv",
            200,
        ),
        Probe(
            ("POST", "/protocols/{design_id}/revisions"),
            f"/protocols/{ids.design}/revisions",
            200,
            kwargs={
                "json": {
                    "document": _design().model_dump(mode="json"),
                    "parent_revision": 1,
                    "change_note": "tightened the base",
                }
            },
        ),
        Probe(
            ("POST", "/protocols/{design_id}/status"),
            f"/protocols/{ids.design}/status",
            204,
            kwargs={
                "json": {"status": "approved", "expected_revision": 2, "expected_status": "draft"}
            },
            last=True,
        ),
        Probe(
            ("GET", "/workflows"),
            "/workflows",
            200,
            (200,),
            own_contains=ids.workflow,
            foreign_excludes=ids.workflow,
        ),
        Probe(
            ("GET", "/workflows/{name}"),
            f"/workflows/{ids.workflow}",
            200,
            own_contains=ids.workflow,
        ),
        Probe(
            ("POST", "/workflows/{name}/approval"),
            f"/workflows/{ids.workflow}/approval",
            204,
            kwargs={"json": {"fingerprint": ids.fingerprint}},
        ),
        Probe(("DELETE", "/workflows/{name}"), f"/workflows/{ids.workflow}", 204, last=True),
    ]


def _routes_of(app: Any) -> set[Route]:
    """Every `(method, path)` the app serves through an `APIRoute`."""
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set())
    }


def test_every_route_has_a_tenant_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route is probed for a leak or neutral for a stated reason; a new one is neither."""
    monkeypatch.setattr("chemclaw.core.config.settings.entra_required", True)
    monkeypatch.setattr("chemclaw.core.config.settings.entra_tenant_id", "t")
    monkeypatch.setattr("chemclaw.core.config.settings.entra_audience", "api://chemclaw")
    served = _routes_of(create_app(connector_factory=_no_connectors))
    probed = {probe.route for probe in _probes(Ids())}
    neutral = set(_NEUTRAL)
    assert not probed & neutral, f"probed and neutral at once: {sorted(probed & neutral)}"
    assert not served - probed - neutral, (
        f"routes with no tenant verdict: {sorted(served - probed - neutral)}. Probe each in "
        "tests/test_tenancy_leak.py (`_probes`), or list it in `_NEUTRAL` with the reason it "
        "reads no tenant's data"
    )
    assert not (probed | neutral) - served, (
        f"verdicts for routes that are gone: {sorted((probed | neutral) - served)}"
    )


class _Handle:
    """What a workflow or schedule handle answers when the broker holds nothing under the id."""

    async def describe(self) -> Any:
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def cancel(self) -> None:
        return None

    async def signal(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Broker:
    """A Temporal client that is up and holds no workflow: a collaborator of the routes."""

    def get_workflow_handle(self, *_args: Any, **_kwargs: Any) -> _Handle:
        return _Handle()

    def get_schedule_handle(self, *_args: Any, **_kwargs: Any) -> _Handle:
        return _Handle()


async def _up() -> _Broker:
    """`temporal_client.connect`, answering."""
    return _Broker()


async def _seed(world: World, monkeypatch: pytest.MonkeyPatch, skills: InMemoryStore) -> Ids:
    """ACME's data, written by ACME's own stores and routes; GLOBEX gets some of its own."""
    ids = Ids(session="s-acme-1", second="s-acme-2")
    for target in (
        "chemclaw.agent.durable_tools.connect",
        "chemclaw.api.routes.pending.connect",
        "chemclaw.durable.schedules.connect",
    ):
        monkeypatch.setattr(target, _up)

    async def _store() -> Any:
        return skills

    for routes in ("skills", "org_skills", "proposals"):
        monkeypatch.setattr(f"chemclaw.api.routes.{routes}.turn_store", _store)

    owners, history = SessionOwnerStore(), PostgresHistoryProvider()
    async with world.acting(ACME):
        for session in (ids.session, ids.second):
            await owners.record(session, PERSON)
            await history.save_messages(
                session, [HumanMessage(f"{MARKER} question"), AIMessage(f"{MARKER} answer")]
            )
    async with world.acting(GLOBEX):
        await owners.record("s-globex-1", PERSON)
        await history.save_messages(
            "s-globex-1", [HumanMessage("globex question"), AIMessage("globex answer")]
        )
    table = {"kind": "table", "columns": [{"key": "a", "label": "A"}], "rows": [{"a": MARKER}]}
    made = world.call(
        ACME,
        "POST",
        f"/sessions/{ids.session}/exhibits",
        json={"kind": "table", "title": f"{MARKER} table", "spec": table},
    )
    assert made.status_code == 201, made.text
    ids.exhibit = made.json()["exhibit_id"]
    member = world.call(ACME, "PUT", f"/sessions/{ids.session}/members/{OTHER}")
    assert member.status_code == 204, member.text

    async with world.acting(ACME):
        ids.ref = await store_tool_result(
            session_id=ids.session,
            correlation_id="c-acme",
            tool="predict",
            text=f"{MARKER} full tool result",
        )
        ids.job = "acme~bo-start-1"
        await PostgresJobRecordSink().record(
            JobRecord(
                job_id=ids.job,
                connector="bo",
                job="start_campaign",
                rationale=f"{MARKER} why",
                requested_by=PERSON,
                summary=f"{MARKER} summary",
            )
        )
        ids.request = "acme~await-1"
        await pending_store.open_request(
            request_id=ids.request,
            kind="question",
            subject=f"{MARKER} subject",
            rationale="why",
            asked_of="",
            requested_by=OTHER,
            session_id=ids.session,
            correlation_id="c-acme",
            due_at=datetime.now(UTC) + timedelta(days=1),
        )
        await record_session_event(
            digest_channel(PERSON),
            DIGEST_KIND,
            {"query": f"{MARKER} query", "note_ids": ["n1"], "headlines": {"n1": MARKER}},
        )
        await record_session_event(
            digest_channel(PERSON),
            CHECK_IN_KIND,
            {"requests": [{"request_id": ids.request, "subject": f"{MARKER} blocked"}]},
        )
        await PostgresArtifactStore().put(
            "acme-key", "geometry.xyz", f"3\n{MARKER}\n".encode(), media_type="chemical/x-xyz"
        )
        body = _skill(ids.proposal, f"{MARKER} proposed")
        ids.proposal_hash = content_hash(body)
        await PostgresProposalStore().propose(
            Proposal(
                kind="skill",
                name=ids.proposal,
                content_hash=ids.proposal_hash,
                content=body,
                rationale=f"{MARKER} because",
                actor=PERSON,
                session_id=ids.session,
                correlation_id="c-acme",
            )
        )
        await save_local_skill(skills, PERSON, ids.skill, _skill(ids.skill, f"{MARKER} mine"))
        house = _skill(ids.org_skill, f"{MARKER} house")
        await save_org_skill(skills, ids.org_skill, house, activated_by=OTHER)
        ids.org_hash = content_hash(house)
        design = _design()
        await PostgresDesignStore().append(
            ids.design, design, run_checks(design), author_kind="human", author=PERSON
        )
        document = _document([_READ_STEP], ids.workflow)
        ids.fingerprint = template_fingerprint(document)
        await PostgresComposedStore().save(
            ComposedWorkflow(
                owner=PERSON, name=ids.workflow, summary=f"{MARKER} routine", document=document
            )
        )
    return ids


def _verdict(world: World, probe: Probe, tenant: str, allowed: tuple[int, ...]) -> str | None:
    """Make `probe` as `tenant`; the complaint if its answer is not an allowed one, else `None`."""
    response = world.call(
        tenant, probe.route[0], probe.path, oid=probe.oid, roles=probe.roles, **probe.kwargs
    )
    where = f"{tenant} {probe.route[0]} {probe.path}"
    if response.status_code not in allowed:
        return f"{where} -> {response.status_code}: {response.text[:200]}"
    if tenant == ACME and probe.own_contains and probe.own_contains not in response.text:
        return f"{where} did not return {probe.own_contains!r}: {response.text[:200]}"
    if tenant == GLOBEX and probe.foreign_excludes and probe.foreign_excludes in response.text:
        return f"{where} returned {probe.foreign_excludes!r}: {response.text[:200]}"
    return None


async def _stored_skills(store: InMemoryStore, tenant: str) -> list[tuple[Any, ...]]:
    """Every item in `store` under a namespace that belongs to `tenant`, as plain data."""
    found: list[tuple[Any, ...]] = []
    for namespace in await store.alist_namespaces():
        first = namespace[0] if namespace else ""
        mine = first.endswith(f"@{tenant}") if tenant != "default" else "@" not in first
        if mine:
            for item in await store.asearch(namespace, limit=1000):
                found.append((namespace, item.key, repr(item.value)))
    return sorted(found)


async def _leaks(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run every probe against `world`; what went wrong, or `[]`."""
    skills = InMemoryStore()
    ids = await _seed(world, monkeypatch, skills)
    probes = _probes(ids)
    failures: list[str] = []

    # 1. ACME reaches its own data: the probes are not vacuous.
    for probe in probes:
        if probe.last or probe.own is None:
            continue
        complaint = _verdict(world, probe, ACME, (probe.own,))
        if complaint:
            failures.append(f"control failed: {complaint}")
    controls = len(world.bodies)

    # 2. GLOBEX names ACME's identifiers.
    before = await world.snapshot(ACME)
    acme_skills = await _stored_skills(skills, ACME)
    for probe in probes:
        complaint = _verdict(world, probe, GLOBEX, probe.foreign)
        if complaint:
            failures.append(f"leak: {complaint}")
    after = await world.snapshot(ACME)

    # 3. Nothing GLOBEX was told contains ACME's marker, and ACME's rows are unchanged.
    mine = [ids.session, ids.second, ids.exhibit, ids.ref, ids.job, ids.request, ids.design]
    for tenant, request, status, body in world.bodies[controls:]:
        if tenant != GLOBEX:
            continue
        if MARKER in body:
            failures.append(f"GLOBEX was shown ACME's data by {request}")
        # An error that echoes the id the caller sent is not a leak; a 2xx naming one is.
        if status < 300 and any(identifier in body for identifier in mine):
            failures.append(f"GLOBEX was told of an ACME identifier by {request}: {body[:200]}")
    for table in before:
        if before[table] != after[table]:
            failures.append(f"ACME's rows in {table} changed under GLOBEX's requests")
    if await _stored_skills(skills, ACME) != acme_skills:
        failures.append("ACME's stored skills changed under GLOBEX's requests")

    # 4. The probes that remove or consume their own target: ACME can do what GLOBEX could not.
    for probe in probes:
        if probe.last and probe.own is not None:
            complaint = _verdict(world, probe, ACME, (probe.own,))
            if complaint:
                failures.append(f"control failed: {complaint}")
    return failures


async def test_no_route_returns_or_changes_another_tenants_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACME's rows are unreachable and untouched through every route, for a person in both."""
    async with two_tenant_world(monkeypatch) as world:
        failures = await _leaks(world, monkeypatch)
    assert not failures, "\n".join(failures)


async def test_the_probes_notice_a_leak_when_the_policies_do_not_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same probes, with the application connecting as the superuser the policies do not bind.

    The control for the test above: a passing run proves something only if this one fails, and on
    the rows, not on a mistake in a probe.
    """
    async with two_tenant_world(monkeypatch, as_superuser=True) as world:
        failures = await _leaks(world, monkeypatch)
    leaks = [failure for failure in failures if failure.startswith(("leak:", "GLOBEX", "ACME"))]
    assert leaks, "the probes found nothing wrong with an application that sees every tenant"
    assert any("GLOBEX was shown ACME's data" in leak for leak in leaks), leaks
