"""The bridge: a verified Teams activity in, one message posted to a shared session as its sender.

The one rule it exists to keep: a message is posted only on a token issued to the person who sent
it. There is no path that posts without a sender's token, and none that posts on another's.

Flow: verify the channel token, name the sender, then either handle a `!command` or look up the
sender's own cached token. No token asks Teams for one (the sign-in card) and posts nothing. A
token in hand posts the message verbatim to the room's bound session and relays what the turn says
back into the room. The model reads nothing the bridge adds: the bridge is a transport.
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx

from chemclaw.rooms.bindings import Binding, BindingStore
from chemclaw.rooms.frontdoor import FrontDoor, FrontDoorRefused
from chemclaw.rooms.identity import (
    AccessToken,
    ExchangeRefused,
    RoomSender,
    TokenCache,
    TokenExchanger,
)
from chemclaw.rooms.teams import (
    Activity,
    BotConnector,
    ChannelAuthError,
    ChannelVerifier,
    UnidentifiedSender,
    message_text,
    room_of,
    sender_of,
    sign_in_activity,
    text_activity,
)

logger = logging.getLogger(__name__)

_HELP = (
    "Say `!new` to start a session for this room, or `!bind <session id>` to use one you can "
    "already reach. `!status` shows the binding and the line, `!unbind` removes it. Anything else "
    "is sent to the session as you, so ChemClaw answers with your own rights. Colleagues must be "
    "added to the session by its owner in ChemClaw before their messages are accepted."
)
_TOKEN_EXCHANGE = "signin/tokenExchange"
_VERIFY_STATE = "signin/verifyState"


@dataclass(frozen=True)
class Reply:
    """What the bridge answers Bot Framework with: an HTTP status and an optional JSON body."""

    status: int = 200
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class BridgeConfig:
    """The values the bridge reads from configuration, handed in so a test can state its own."""

    tenant_id: str
    sso_connection_name: str
    sso_resource_uri: str
    max_inflight: int
    reply_max_chars: int


@dataclass
class Bridge:
    """Everything one Teams bridge needs; `handle` is its only entry."""

    verifier: ChannelVerifier
    exchanger: TokenExchanger
    tokens: TokenCache
    bindings: BindingStore
    front_door: FrontDoor
    connector: BotConnector
    config: BridgeConfig
    _running: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def handle(self, authorization: str | None, raw: dict[str, Any]) -> Reply:
        """Process one inbound activity; a long turn continues in the background."""
        try:
            activity = Activity.model_validate(raw)
        except ValueError:
            return Reply(400)
        try:
            await self.verifier.verify(authorization, activity)
        except ChannelAuthError as exc:
            logger.info("an activity failed channel authentication: %s", exc)
            return Reply(401)
        try:
            sender = sender_of(activity, tenant_id=self.config.tenant_id)
        except UnidentifiedSender as exc:
            await self._say(activity, exc.reason)
            return Reply()
        if activity.type == "invoke":
            return await self._invoke(activity, sender)
        if activity.type == "message":
            await self._message(activity, sender)
        return Reply()

    async def drain(self) -> None:
        """Wait for every background message to finish (shutdown and tests)."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    # --- sign-in ---------------------------------------------------------------------------------

    async def _invoke(self, activity: Activity, sender: RoomSender) -> Reply:
        """Teams's silent sign-in: exchange the token it hands over for the sender's own."""
        if activity.name == _VERIFY_STATE:
            return Reply()
        if activity.name != _TOKEN_EXCHANGE:
            return Reply(501)
        value = activity.value or {}
        failure = {"id": value.get("id", ""), "connectionName": value.get("connectionName", "")}
        assertion = value.get("token")
        if value.get("connectionName") != self.config.sso_connection_name or not isinstance(
            assertion, str
        ):
            return Reply(412, {**failure, "failureDetail": "unexpected sign-in request"})
        try:
            token = await self.exchanger.exchange(sender, assertion)
        except ExchangeRefused as exc:
            await self._say(activity, exc.reason)
            return Reply(412, {**failure, "failureDetail": exc.reason})
        self.tokens.put(sender, token)
        await self._say(activity, f"Signed in as {sender.name or 'you'}. Send your message again.")
        return Reply(200, failure)

    async def _ask_to_sign_in(self, activity: Activity) -> None:
        """Offer the sign-in card; nothing is posted to ChemClaw."""
        await self._send(
            activity,
            sign_in_activity(
                connection_name=self.config.sso_connection_name,
                resource_uri=self.config.sso_resource_uri,
                reply_to=activity.id,
            ),
        )

    # --- messages and commands -------------------------------------------------------------------

    async def _message(self, activity: Activity, sender: RoomSender) -> None:
        """A person's message: a command, or text for the room's session, on their own token."""
        text = message_text(activity)
        if not text:
            await self._say(activity, _HELP)
            return
        token = self.tokens.get(sender)
        if token is None:
            await self._ask_to_sign_in(activity)
            return
        if text.startswith("!"):
            await self._command(activity, sender, token, text)
            return
        binding = self.bindings.get(room_of(activity))
        if binding is None:
            await self._say(activity, "This room has no session yet. " + _HELP)
            return
        if len(self._running) >= self.config.max_inflight:
            await self._say(activity, "The bridge is busy answering other messages; try again.")
            return
        self._spawn(self._post(activity, sender, token, binding.session_id, text))

    async def _command(
        self, activity: Activity, sender: RoomSender, token: AccessToken, text: str
    ) -> None:
        """`!new`, `!bind`, `!unbind`, `!status`, `!help`: each runs as the person who typed it."""
        word, _, argument = text.partition(" ")
        room = room_of(activity)
        argument = argument.strip()
        try:
            if word == "!new":
                session_id = await self.front_door.create_session(token)
                self.bindings.bind(room, Binding(session_id, sender.object_id))
                await self._say(
                    activity,
                    f"Session `{session_id}` is bound to this room. You own it: add colleagues "
                    "in ChemClaw before they write here.",
                )
            elif word == "!bind" and argument:
                if not await self.front_door.can_reach(token, argument):
                    await self._say(
                        activity, "You cannot reach that session, so I did not bind it."
                    )
                    return
                self.bindings.bind(room, Binding(argument, sender.object_id))
                await self._say(activity, f"Session `{argument}` is bound to this room.")
            elif word == "!unbind":
                done = self.bindings.unbind(room, by=sender.object_id)
                await self._say(
                    activity,
                    "Unbound." if done else "Only the person who bound this room can unbind it.",
                )
            elif word == "!status":
                await self._status(activity, token, room)
            else:
                await self._say(activity, _HELP)
        except FrontDoorRefused as exc:
            await self._refused(activity, sender, exc)
        except httpx.HTTPError:
            await self._say(activity, "ChemClaw did not answer; try again shortly.")

    async def _status(self, activity: Activity, token: AccessToken, room: str) -> None:
        """Say which session the room is bound to and how many messages wait for it."""
        binding = self.bindings.get(room)
        if binding is None:
            await self._say(activity, "This room has no session yet. " + _HELP)
            return
        waiting = await self.front_door.waiting(token, binding.session_id)
        await self._say(
            activity, f"Bound to session `{binding.session_id}`; {waiting} message(s) waiting."
        )

    # --- the turn --------------------------------------------------------------------------------

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """Run `work` in the background, held so it is neither collected nor lost at shutdown."""
        task = asyncio.ensure_future(work)
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _post(
        self, activity: Activity, sender: RoomSender, token: AccessToken, session_id: str, text: str
    ) -> None:
        """Post the message as its sender and relay the turn's events into the room."""
        announced: set[tuple[str, object]] = set()
        try:
            async for event in self.front_door.post_message(token, session_id, text):
                note = self._note(event, sender, announced)
                if note:
                    await self._say(activity, note)
        except FrontDoorRefused as exc:
            await self._refused(activity, sender, exc)
        except httpx.HTTPError:
            await self._say(activity, "ChemClaw stopped answering before the turn finished.")
        except Exception:
            logger.exception("a room message failed")
            await self._say(activity, "Something went wrong in the bridge; nothing more was sent.")

    def _note(
        self, event: dict[str, Any], sender: RoomSender, announced: set[tuple[str, object]]
    ) -> str:
        """What a turn event says to the room, or `""` for the many that say nothing."""
        kind = event.get("type")
        who = sender.name or "your"
        if kind == "queued" and event.get("ticket") is not None:
            place = int(event.get("position") or 0) + 1
            return f"{who}: waiting in the session's line, place {place}."
        if kind == "turn_started":
            return f"{who}: your message is now running."
        if kind == "tool_queued" and event.get("state") == "queued":
            key = ("tool", event.get("job_id"))
            if key in announced:
                return ""
            announced.add(key)
            return f"`{event.get('tool', 'a tool')}` is waiting for a compute slot."
        if kind == "answer":
            return str(event.get("text", ""))[: self.config.reply_max_chars]
        if kind == "question":
            options = event.get("options") or []
            asked = str(event.get("question", ""))
            return asked + (" Options: " + ", ".join(map(str, options)) if options else "")
        if kind == "approval_request":
            return "A plan is waiting for your approval in ChemClaw; approve it there."
        if kind == "error":
            return f"The turn ended: {event.get('message', 'it failed')}"
        return ""

    async def _refused(self, activity: Activity, sender: RoomSender, exc: FrontDoorRefused) -> None:
        """Tell the room, in a fixed sentence, why ChemClaw turned the sender's request away."""
        if exc.status == 401:
            self.tokens.drop(sender)
            await self._ask_to_sign_in(activity)
        elif exc.status in {403, 404}:
            await self._say(
                activity,
                "You are not a member of this room's session. Ask its owner to add you.",
            )
        elif exc.status == 409:
            await self._say(
                activity, "You already have a message waiting, or the line is full; try again."
            )
        elif exc.status == 429:
            await self._say(activity, "ChemClaw is busy right now; try again shortly.")
        else:
            await self._say(activity, "ChemClaw refused the message.")

    # --- replying --------------------------------------------------------------------------------

    async def _say(self, activity: Activity, text: str) -> None:
        """Reply in the activity's conversation with `text`."""
        await self._send(activity, text_activity(text, reply_to=activity.id))

    async def _send(self, activity: Activity, body: dict[str, Any]) -> None:
        """Deliver `body` to the activity's conversation as the bot."""
        await self.connector.send(activity.service_url, activity.conversation.id, body)
