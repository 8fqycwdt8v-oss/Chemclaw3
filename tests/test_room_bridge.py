"""The Teams room bridge posts a message as its sender, on the sender's own token, or not at all.

`D-2026-10-24-a-chat-room-reaches-a-shared-session-only-as-its-senders`. The ChemClaw front door is
the real app, validating real (fake-tenant-signed) tokens, so who a message ran as is read from
inside the turn. Teams and Entra are `tests.fakes_teams`. Every guard below has been watched failing
under the mutation named in its docstring.
"""

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from chemclaw.agent import plan_approval_store as store_module
from chemclaw.agent import session_members as members_module
from chemclaw.api import auth
from chemclaw.api.routes import turns as turns_module
from chemclaw.core.config import Settings, settings
from chemclaw.rooms import serve
from chemclaw.rooms.bindings import Binding, BindingStore
from chemclaw.rooms.bridge import Bridge, Reply
from chemclaw.rooms.frontdoor import FrontDoor
from chemclaw.rooms.identity import (
    AccessToken,
    EntraOnBehalfOf,
    ExchangeRefused,
    RoomSender,
    TokenCache,
)
from chemclaw.rooms.teams import BotConnector
from tests.fakes_teams import (
    API_AUDIENCE,
    API_ISSUER,
    API_SCOPE,
    BOT_APP,
    BOT_SECRET,
    BOT_SECRET_ENV,
    BOT_TOKEN,
    CONNECTION,
    CONNECTOR_SCOPE,
    METADATA_URL,
    SERVICE_URL,
    SSO_RESOURCE,
    TENANT,
    TOKEN_ENDPOINT,
    Person,
    World,
    activity,
)
from tests.test_detach import _Served
from tests.test_service import _app, _FakeOwnerStore
from tests.test_session_turn_queue import _Ledger, _started

_ANA = Person("ana-room", "Ana", frozenset({"process-chemist"}))
_BEN = Person("ben-room", "Ben", frozenset({"analyst"}))
_CAT = Person("cat-room", "Cat", frozenset({"qa-reviewer"}))
_ROOM = "19:room@thread.tacv2"


def _sender(person: Person) -> RoomSender:
    return RoomSender(tenant_id=TENANT, object_id=person.oid, name=person.name)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The fake tenant, with the real front door told to trust its signing key."""
    fake = World(people={p.oid: p for p in (_ANA, _BEN, _CAT)})
    monkeypatch.setenv(BOT_SECRET_ENV, BOT_SECRET)
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_audience", API_AUDIENCE)
    monkeypatch.setattr(settings, "entra_issuer", API_ISSUER)
    monkeypatch.setattr(auth, "_signing_key", lambda _token: fake.entra_key.public_key())
    monkeypatch.setattr(turns_module, "idle_poll_seconds", lambda: 0.05)
    members_module.session_member_store.cache_clear()
    store_module.plan_approval_store.cache_clear()
    yield fake
    members_module.session_member_store.cache_clear()
    store_module.plan_approval_store.cache_clear()


def _config(tmp_path: Path) -> Settings:
    """The bridge's settings, stated in full and local to the test."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        teams_bridge_enabled=True,
        teams_bot_app_id=BOT_APP,
        teams_bot_credential_token_env=BOT_SECRET_ENV,
        teams_tenant_id=TENANT,
        teams_authority_url="https://login.test",
        teams_api_base_url="http://core.test:8080",
        teams_api_scope=API_SCOPE,
        teams_sso_connection_name=CONNECTION,
        teams_sso_resource_uri=SSO_RESOURCE,
        teams_openid_metadata_url=METADATA_URL,
        teams_service_url_hosts="smba.test",
        teams_bindings_file=str(tmp_path / "bindings.json"),
    )


class _Rig:
    """A bridge over the fake Teams and Entra and a real front door at `base`."""

    def __init__(self, base: str, world: World, tmp_path: Path) -> None:
        self.world = world
        self.seen: list[tuple[str, str, str, str]] = []
        transport = world.transport()
        self.api = httpx.AsyncClient(
            base_url=base, timeout=30, trust_env=False, event_hooks={"request": [self._record]}
        )
        self.bridge: Bridge = serve.build_bridge(
            _config(tmp_path),
            entra=httpx.AsyncClient(transport=transport),
            teams=httpx.AsyncClient(transport=transport),
            api=self.api,
        )

    async def _record(self, request: httpx.Request) -> None:
        """What the front door was sent: method, path, bearer and body."""
        self.seen.append(
            (
                request.method,
                request.url.path,
                request.headers.get("Authorization", ""),
                request.content.decode() if request.content else "",
            )
        )

    async def send(self, raw: dict[str, Any], *, authorization: str | None = "default") -> Reply:
        """Deliver one activity as Teams would, then wait for any background turn to finish."""
        header = (
            f"Bearer {self.world.channel_token()}" if authorization == "default" else authorization
        )
        reply = await self.bridge.handle(header, raw)
        await self.bridge.drain()
        return reply

    async def say(self, person: Person, text: str, **kwargs: Any) -> Reply:
        """`person` types `text` in the room, mentioning the bot as Teams would."""
        return await self.send(activity(person, f"<at>ChemClaw</at> {text}", room=_ROOM, **kwargs))

    async def sign_in(self, person: Person) -> Reply:
        """Teams's silent sign-in for `person`: the invoke carrying their token for the bot."""
        return await self.send(
            activity(
                person,
                kind="invoke",
                name="signin/tokenExchange",
                room=_ROOM,
                value={
                    "id": "exchange-1",
                    "connectionName": CONNECTION,
                    "token": self.world.sso_token(person),
                },
            )
        )

    def posted(self) -> list[tuple[str, str, str]]:
        """Every message the bridge posted to a session: (path, bearer, message text)."""
        return [
            (path, bearer, json.loads(body)["message"])
            for method, path, bearer, body in self.seen
            if method == "POST" and path.endswith("/messages")
        ]

    async def close(self) -> None:
        await self.api.aclose()


def _served(agent: _Ledger) -> _Served:
    return _Served(_app(agent, owner_store=_FakeOwnerStore()))


# --- a message is posted as its sender ------------------------------------------------------------


def test_a_message_runs_as_its_sender_on_the_senders_own_token(
    world: World, tmp_path: Path
) -> None:
    """Ana's message runs as Ana with Ana's roles, Ben's as Ben; each used only their own token.

    Mutation: `_post` passing the room's first-signed-in token instead of the sender's.
    """
    agent = _Ledger()

    async def _run() -> _Rig:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            await rig.sign_in(_ANA)
            await rig.sign_in(_BEN)
            await rig.say(_ANA, "!new")
            session_id = rig.bridge.bindings.get(_ROOM).session_id  # type: ignore[union-attr]
            admitted = await rig.api.put(
                f"/sessions/{session_id}/members/{_BEN.oid}",
                headers={"Authorization": f"Bearer {rig.bridge.tokens.get(_sender(_ANA)).value}"},  # type: ignore[union-attr]
            )
            assert admitted.status_code == 204, admitted.text
            await rig.say(_ANA, "hello from ana")
            await rig.say(_BEN, "hello from ben")
            served.wait_for_slot_release(session_id)
            return rig

    rig = asyncio.run(_run())

    assert agent.ran == [
        ("hello from ana", _ANA.oid, _ANA.roles),
        ("hello from ben", _BEN.oid, _BEN.roles),
    ], "a room message ran as somebody other than its sender"
    for _path, bearer, message in rig.posted():
        sender = _ANA if "ana" in message else _BEN
        assert world.minted[bearer.removeprefix("Bearer ")] == sender.oid
    assert len(world.exchanges) == 2, "each person was exchanged for once"
    texts = world.room_texts()
    assert any("done hello from ana" in t for t in texts), texts
    assert any("done hello from ben" in t for t in texts), texts


def test_the_message_is_posted_verbatim_and_the_bridge_adds_nothing_the_model_reads(
    world: World, tmp_path: Path
) -> None:
    """What the model reads is what the person typed, without Teams's mention markup.

    The bridge is a transport: no prefix naming the room, the sender or the platform.
    """
    agent = _Ledger()

    async def _run() -> list[str]:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            await rig.sign_in(_ANA)
            await rig.say(_ANA, "!new")
            await rig.say(_ANA, "what is the pKa of acetic acid?  ")
            return [message for _p, _b, message in rig.posted()]

    assert asyncio.run(_run()) == ["what is the pKa of acetic acid?"]
    assert [m for m, _a, _r in agent.ran] == ["what is the pKa of acetic acid?"]


def test_a_waiting_message_tells_the_room_its_place_and_when_it_runs(
    world: World, tmp_path: Path
) -> None:
    """Ben, behind Ana's running turn, is told his place, then that he is running, then answered."""
    agent = _Ledger()

    async def _until_told(rig: _Rig, needle: str) -> None:
        deadline = time.monotonic() + 15
        while not any(needle in t for t in rig.world.room_texts()):
            assert time.monotonic() < deadline, rig.world.room_texts()
            await asyncio.sleep(0.02)

    async def _run() -> list[str]:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            for person in (_ANA, _BEN):
                await rig.sign_in(person)
            await rig.say(_ANA, "!new")
            session_id = rig.bridge.bindings.get(_ROOM).session_id  # type: ignore[union-attr]
            await rig.api.put(
                f"/sessions/{session_id}/members/{_BEN.oid}",
                headers={"Authorization": f"Bearer {rig.bridge.tokens.get(_sender(_ANA)).value}"},  # type: ignore[union-attr]
            )
            ana = asyncio.create_task(rig.say(_ANA, "hold ana"))
            while not await _started(agent, 1):
                await asyncio.sleep(0.02)
            ben = asyncio.create_task(rig.say(_BEN, "ben asks"))
            await _until_told(rig, "place 1")
            agent.gate.set()
            await asyncio.gather(ana, ben)
            served.wait_for_slot_release(session_id)
            return rig.world.room_texts()

    texts = asyncio.run(_run())
    ben_lines = [t for t in texts if t.startswith("Ben:") or t.endswith("done ben asks")]
    assert ben_lines[0].startswith("Ben: waiting in the session's line, place 1"), texts
    assert ben_lines[1] == "Ben: your message is now running.", texts
    assert ben_lines[-1].endswith("done ben asks"), texts
    assert [m for m, _a, _r in agent.ran] == ["hold ana", "ben asks"]


def test_a_person_the_session_does_not_admit_is_told_so_and_nothing_runs(
    world: World, tmp_path: Path
) -> None:
    """Being in the Teams room grants nothing: the front door's membership check still decides."""
    agent = _Ledger()

    async def _run() -> World:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            await rig.sign_in(_ANA)
            await rig.sign_in(_CAT)
            await rig.say(_ANA, "!new")
            await rig.say(_CAT, "let me in")
            return rig.world

    texts = asyncio.run(_run()).room_texts()
    assert any("not a member" in t for t in texts), texts
    assert agent.ran == [], "a person who is not a member ran a turn"


# --- an unverifiable sender is refused, and nothing is posted as the bot --------------------------


def _foreign_tenant(person: Person) -> dict[str, Any]:
    return activity(person, "hi", room=_ROOM, tenant="another-tenant")


def _disagreeing_tenants(person: Person) -> dict[str, Any]:
    raw = activity(person, "hi", room=_ROOM)
    raw["conversation"]["tenantId"] = "another-tenant"
    return raw


_REFUSED_BEFORE_ANY_WORK: dict[
    str, Callable[[World, Person], tuple[dict[str, Any], str | None]]
] = {
    "no channel token": (lambda w, p: (activity(p, "hi", room=_ROOM), None)),
    "a token for another bot": (
        lambda w, p: (
            activity(p, "hi", room=_ROOM),
            f"Bearer {w.channel_token(audience='another-bot')}",
        )
    ),
    "a token for another service": (
        lambda w, p: (
            activity(p, "hi", room=_ROOM),
            f"Bearer {w.channel_token(service_url='https://elsewhere.test/')}",
        )
    ),
    "an expired token": (
        lambda w, p: (
            activity(p, "hi", room=_ROOM),
            f"Bearer {w.channel_token(lifetime=-30)}",
        )
    ),
    "a token signed by someone else": (
        lambda w, p: (
            activity(p, "hi", room=_ROOM),
            f"Bearer {w.sso_token(p)}",
        )
    ),
    "a sender of another tenant": (
        lambda w, p: (_foreign_tenant(p), f"Bearer {w.channel_token()}")
    ),
    "tenants that disagree": (
        lambda w, p: (_disagreeing_tenants(p), f"Bearer {w.channel_token()}")
    ),
    "no Entra object id": (
        lambda w, p: (
            activity(p, "hi", room=_ROOM, aad_object_id=""),
            f"Bearer {w.channel_token()}",
        )
    ),
}


@pytest.mark.parametrize("case", sorted(_REFUSED_BEFORE_ANY_WORK))
def test_an_unverifiable_sender_is_refused_and_nothing_is_posted(
    case: str, world: World, tmp_path: Path
) -> None:
    """No activity that cannot be pinned on a person of this tenant posts or exchanges anything.

    Not as that person, and not as the bot.

    Mutations: `sender_of` skipping the tenant check; `verify` skipping the audience, the
    `serviceurl` comparison or the expiry; `handle` ignoring `ChannelAuthError`.
    """
    agent = _Ledger()
    raw, header = _REFUSED_BEFORE_ANY_WORK[case](world, _ANA)

    async def _run() -> tuple[Reply, _Rig]:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            rig.bridge.bindings.bind(_ROOM, Binding("anything", _ANA.oid))
            rig.bridge.tokens.put(_sender(_ANA), AccessToken("held", time.time() + 3600))
            reply = await rig.send(raw, authorization=header)
            return reply, rig

    reply, rig = asyncio.run(_run())
    assert rig.posted() == [] and agent.ran == [], f"{case}: a message was posted"
    assert world.exchanges == [], f"{case}: a token was exchanged"
    assert all(bearer != f"Bearer {BOT_TOKEN}" for _m, _p, bearer, _b in rig.seen), (
        f"{case}: the bot's token reached ChemClaw"
    )
    assert reply.status in {200, 400, 401}
    assert world.sign_in_cards() == 0, f"{case}: an unidentified sender was offered a sign-in"


def test_a_person_who_has_not_signed_in_is_asked_to_and_nothing_is_posted(
    world: World, tmp_path: Path
) -> None:
    """No token for the sender means a sign-in card, never a post under another credential.

    Mutation: `_message` falling back to any held token (here Ana's) when the sender has none.
    """
    agent = _Ledger()

    async def _run() -> _Rig:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            await rig.sign_in(_ANA)
            await rig.say(_ANA, "!new")
            await rig.say(_BEN, "hello, I never signed in")
            return rig

    rig = asyncio.run(_run())
    assert world.sign_in_cards() == 1
    assert agent.ran == [] and rig.posted() == []
    card = next(r for r in world.replies if r.get("attachments"))["attachments"][0]
    assert card["content"]["connectionName"] == CONNECTION
    assert card["content"]["tokenExchangeResource"]["uri"] == SSO_RESOURCE


def test_a_sign_in_token_that_is_not_the_senders_is_refused_before_entra_sees_it(
    world: World, tmp_path: Path
) -> None:
    """Ben's token presented in an activity from Ana is refused, and nothing is held for either.

    Mutation: `EntraOnBehalfOf.exchange` without its `_belongs_to(assertion, sender)` check.
    """

    async def _run() -> tuple[Reply, _Rig]:
        with _served(_Ledger()) as served:
            rig = _Rig(served.base, world, tmp_path)
            reply = await rig.send(
                activity(
                    _ANA,
                    kind="invoke",
                    name="signin/tokenExchange",
                    room=_ROOM,
                    value={
                        "id": "x",
                        "connectionName": CONNECTION,
                        "token": world.sso_token(_BEN),
                    },
                )
            )
            return reply, rig

    reply, rig = asyncio.run(_run())
    assert reply.status == 412
    assert world.exchanges == []
    assert len(rig.bridge.tokens) == 0


def test_an_exchange_that_returns_someone_elses_token_is_discarded(
    world: World, tmp_path: Path
) -> None:
    """If Entra (or a proxy in front of it) answered with another person's token, it is not held.

    Mutation: `EntraOnBehalfOf.exchange` without its `_belongs_to(value, sender)` check.
    """
    world.mint_for_instead = _BEN.oid

    async def _run() -> tuple[Reply, _Rig]:
        with _served(_Ledger()) as served:
            rig = _Rig(served.base, world, tmp_path)
            return await rig.sign_in(_ANA), rig

    reply, rig = asyncio.run(_run())
    assert reply.status == 412
    assert len(world.exchanges) == 1
    assert rig.bridge.tokens.get(_sender(_ANA)) is None


def test_the_bridge_holds_no_token_that_could_post_as_another_sender(
    world: World, tmp_path: Path
) -> None:
    """Each held token names its holder; the bot's own credential is good for Teams alone.

    The bot's client-credentials grant is asked for the Bot Framework scope and nothing else (the
    fake refuses it for ChemClaw's API, as an app with no app role is refused), and it is the only
    credential the bridge holds that is not a sender's.

    Mutation: `BotConnector` built with the API scope instead of the connector scope.
    """

    async def _run() -> _Rig:
        with _served(_Ledger()) as served:
            rig = _Rig(served.base, world, tmp_path)
            await rig.sign_in(_ANA)
            await rig.sign_in(_BEN)
            await rig.say(_ANA, "!new")
            await rig.say(_BEN, "hello")  # refused by the session, which replies as the bot
            return rig

    rig = asyncio.run(_run())
    for person in (_ANA, _BEN):
        held = rig.bridge.tokens.get(_sender(person))
        assert held is not None and world.minted[held.value] == person.oid
    assert rig.bridge.tokens.get(_sender(_CAT)) is None
    assert world.credential_grants, "no reply was ever sent as the bot"
    assert all(grant["scope"] == CONNECTOR_SCOPE for grant in world.credential_grants)
    assert world.replies, "the bot's reply never reached Teams"


# --- configuration, start-up and the pieces ------------------------------------------------------


def test_the_bridge_is_off_by_default_and_an_enabled_one_must_be_complete() -> None:
    """Nothing starts unless an operator says so, and a half-configured bridge refuses to start."""
    assert Settings(_env_file=None).teams_bridge_enabled is False  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="teams_bot_app_id"):
        Settings(_env_file=None, teams_bridge_enabled=True)  # type: ignore[call-arg]
    with pytest.raises(SystemExit, match="Teams bridge is off"):
        serve.main()


def test_the_bridge_will_not_start_against_a_front_door_that_accepts_any_token() -> None:
    """A front door with enforcement off answers a bogus bearer 200, so start-up refuses.

    Every message would be one stand-in principal's. One that answers 401 is accepted.
    """

    def _door(status: int) -> FrontDoor:
        transport = httpx.MockTransport(lambda _request: httpx.Response(status))
        return FrontDoor(
            httpx.AsyncClient(base_url="http://core.test", transport=transport),
            stream_timeout=httpx.Timeout(5),
        )

    with pytest.raises(SystemExit, match="does not turn an invalid token away"):
        asyncio.run(serve.refuse_unenforcing_front_door(_door(200)))
    asyncio.run(serve.refuse_unenforcing_front_door(_door(401)))


def test_a_reply_is_never_sent_to_a_host_nobody_listed(world: World) -> None:
    """An activity cannot aim the bot's reply token at a host of its choosing."""

    async def _run() -> tuple[bool, bool]:
        client = httpx.AsyncClient(transport=world.transport())
        connector = BotConnector(
            client,
            token_endpoint=TOKEN_ENDPOINT,
            app_id=BOT_APP,
            secret_env=BOT_SECRET_ENV,
            scope=CONNECTOR_SCOPE,
            service_hosts=["smba.test"],
        )
        elsewhere = await connector.send("https://evil.test/", "c", {"type": "message"})
        listed = await connector.send(SERVICE_URL, "c", {"type": "message"})
        return elsewhere, listed

    elsewhere, listed = asyncio.run(_run())
    assert (elsewhere, listed) == (False, True)
    assert world.unlisted == [], "the bot's token was sent to a host nobody listed"
    assert world.replies == [{"type": "message"}]


def test_an_exchange_asks_entra_for_the_on_behalf_of_grant_and_the_api_scope(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire is Entra's: the jwt-bearer grant, the sender's assertion, ChemClaw's scope."""
    monkeypatch.setenv(BOT_SECRET_ENV, BOT_SECRET)
    exchanger = EntraOnBehalfOf(
        httpx.AsyncClient(transport=world.transport()),
        token_endpoint=TOKEN_ENDPOINT,
        app_id=BOT_APP,
        secret_env=BOT_SECRET_ENV,
        scope=API_SCOPE,
    )
    token = asyncio.run(exchanger.exchange(_sender(_ANA), world.sso_token(_ANA)))
    assert world.minted[token.value] == _ANA.oid
    (form,) = world.exchanges
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["requested_token_use"] == "on_behalf_of" and form["scope"] == API_SCOPE
    monkeypatch.setenv(BOT_SECRET_ENV, "")
    with pytest.raises(ExchangeRefused, match="not configured"):
        asyncio.run(exchanger.exchange(_sender(_ANA), world.sso_token(_ANA)))


def test_a_cached_token_is_dropped_before_it_expires_and_the_cache_is_bounded() -> None:
    """A message never starts a turn on a credential about to lapse, and memory holds a bound."""
    now = [1000.0]
    cache = TokenCache(max_entries=2, leeway_seconds=100, clock=lambda: now[0])
    first, second, third = (RoomSender(TENANT, oid) for oid in ("a", "b", "c"))
    cache.put(first, AccessToken("t-a", 1500))
    now[0] = 1399
    assert cache.get(first) is not None
    now[0] = 1401
    assert cache.get(first) is None and len(cache) == 0
    for person in (first, second, third):
        cache.put(person, AccessToken(f"t-{person.object_id}", 9999))
    assert cache.get(first) is None and cache.get(third) is not None


def test_a_binding_survives_a_restart_and_only_its_binder_removes_it(tmp_path: Path) -> None:
    """Bindings are a file replaced atomically; the room's binder alone may unbind."""
    path = str(tmp_path / "state" / "bindings.json")
    store = BindingStore(path)
    store.bind(_ROOM, Binding("session-1", "ana-room"))
    assert BindingStore(path).get(_ROOM) == Binding("session-1", "ana-room")
    assert store.unbind(_ROOM, by="ben-room") is False
    assert BindingStore(path).get(_ROOM) is not None
    assert store.unbind(_ROOM, by="ana-room") is True
    assert BindingStore(path).get(_ROOM) is None
    assert [p.name for p in Path(path).parent.iterdir()] == ["bindings.json"]


def test_a_room_binds_only_a_session_its_binder_can_reach(world: World, tmp_path: Path) -> None:
    """`!bind` is checked with the binder's own token: a session they cannot reach is not bound."""
    agent = _Ledger()

    async def _run() -> _Rig:
        with _served(agent) as served:
            rig = _Rig(served.base, world, tmp_path)
            await rig.sign_in(_ANA)
            await rig.sign_in(_BEN)
            await rig.say(_ANA, "!new")
            ana_session = rig.bridge.bindings.get(_ROOM).session_id  # type: ignore[union-attr]
            await rig.send(activity(_BEN, f"!bind {ana_session}", room="19:other@thread.tacv2"))
            return rig

    rig = asyncio.run(_run())
    assert rig.bridge.bindings.get("19:other@thread.tacv2") is None
    assert any("cannot reach that session" in t for t in world.room_texts()), world.room_texts()


def test_the_egress_guard_admits_the_bridges_hosts_only_when_the_bridge_is_on(
    tmp_path: Path,
) -> None:
    """No process gains a Microsoft host by default; an enabled bridge dials exactly its own four.

    Mutation: `derive_allowed` adding them unconditionally.
    """
    from chemclaw.core import netguard

    off = netguard.derive_allowed(Settings(_env_file=None))  # type: ignore[call-arg]
    on = netguard.derive_allowed(_config(tmp_path))
    shipped = {"login.microsoftonline.com", "login.botframework.com", "smba.trafficmanager.net"}
    assert not shipped & off, "a process with the bridge off was given a Microsoft host"
    assert {"login.test", "channel.test", "smba.test"} <= on
