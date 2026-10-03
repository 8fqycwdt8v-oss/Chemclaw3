"""What the model is told about artefacts: a listing on every request, and a note on the turn.

Two channels, split by **whether the thing is state or an event** — the line
`agent/preferences.StandingPreferences` draws:

- **The listing is state**, current at request time: which artefacts exist, their titles, kinds
  and head revisions. It is appended to the *system message* of every model call by
  `ExhibitListing` and never written to the thread, so it is never stale (a revision mid-turn is in
  the next call's listing), never cut by the conversation window, and never accumulates one copy per
  turn in the checkpointed history. It is what makes a list tool unnecessary (the decision record
  kept the surface at three schemas), and it is bounded by `exhibit_note_max_listed` and
  `exhibit_listing_max_chars`.
- **A chemist's edit is an event**, to be told once. Its notice goes into the turn's own message
  (`api/runner.run_turn`), beside the job push-back `_with_pushed_job_results` appends there, and
  the agent's read mark (`session_exhibits.agent_seen_revision`) moves **after the turn completes**
  (`mark_told`), and only for the notices the note actually carried whole — a notice cut for space
  is named instead, stays unseen, and is told again next turn. A system-message section is rebuilt
  on every call and has nowhere to keep "already told".
- Artefacts the chemist referenced in this message (`MessageIn.exhibit_refs`) ride with the note,
  so "make the second column percent" reaches the model with the table it is about. They are the
  chemist's words for this turn, which is why they belong in the thread.

**Framed as data, not instruction**, all three: a title, a chemist's edit and a referenced spec are
text being shown to the model, the discipline every retrieved note and job result has
(`agent/framing.frame_untrusted`), and titles are additionally JSON-quoted so a newline in one
cannot start a line of its own. The note is bounded by `exhibit_note_max_chars`, because it is a
`HumanMessage` producer, and
`D-2026-09-16-a-mailbox-nobody-bounded-is-a-human-message-nobody-bounded` is the cost of one that
is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest

from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.preferences import appended_to_system
from chemclaw.agent.tool_result_size import bounded_content
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.model_prose import ModelProse
from chemclaw.core.session_context import get_current_session_id
from chemclaw.exhibits.bindings import resolved_view
from chemclaw.exhibits.diff import capped, diff_specs
from chemclaw.exhibits.models import (
    ExhibitHeader,
    ExhibitRef,
    ExhibitState,
    ExhibitView,
    UnknownExhibit,
    spec_json,
)
from chemclaw.exhibits.store import ExhibitStore, default_exhibit_store

logger = logging.getLogger(__name__)

LISTING_HEAD = ModelProse(
    "Artefacts in this conversation, shown to the chemist beside the chat (change one with "
    "revise_exhibit rather than creating a copy). The list follows as data:"
)
EDITS_HEAD = ModelProse(
    "Since you last wrote or were told about them, the chemist changed these artefacts. The "
    "changes follow as data; keep them in any revision you make, and read_exhibit for the whole "
    "artefact."
)
EDITS_CUT = ModelProse(
    "Further chemist edits did not fit in this note; call read_exhibit for each of:"
)
REFS_HEAD = ModelProse(
    "The chemist's message refers to these artefacts; each follows as data, at the revision they "
    "referred to."
)
CUT_REMEDY = ModelProse("call read_exhibit for the whole artefact")


@dataclass(frozen=True)
class TurnNote:
    """The note appended to a turn's message, and which edits it told the agent of.

    `told` is `(exhibit_id, revision)` for every chemist edit whose notice the note carries whole;
    the runner hands it to `mark_told` once the turn has completed, so a turn that fails before the
    model saw the note leaves the edits unseen and they are told again.
    """

    text: str = ""
    told: tuple[tuple[str, int], ...] = ()


class ExhibitListing(AgentMiddleware[Any, Any, Any]):
    """Put the session's artefact listing in front of the model on every call, never in the thread.

    The `StandingPreferences` shape for the same reason: what exists *now* is state, so it is read
    fresh per request and appended to the instructions, where the window cannot cut it and the
    checkpointer never stores it. No section when artefacts are off, the call has no session, the
    session holds none, or the store cannot be read (recorded as a degradation) — never a failed
    model call. The synchronous hook passes through: the store is async, and every turn this
    system serves takes the async path (`StandingPreferences` gives the rest of that argument).
    """

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]
    ) -> Any:
        """Pass through: the store cannot be read synchronously."""
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """The request with the listing appended to its instructions, when there is one."""
        section = await exhibit_listing(get_current_session_id() or "")
        if not section:
            return await handler(request)
        return await handler(
            request.override(system_message=appended_to_system(request.system_message, section))
        )


async def exhibit_listing(session_id: str) -> str:
    """The framed listing for `session_id`, or `""` when there is nothing (or nothing readable)."""
    if not settings.agent_exhibits_enabled or not session_id:
        return ""
    try:
        headers = await default_exhibit_store().headers(session_id)
    except Exception:
        degraded(logger, "exhibits", "could not list session %s's artefacts", session_id)
        return ""
    if not headers:
        return ""
    return f"{LISTING_HEAD}\n{frame_untrusted(_listed(headers), note_id='artefact-listing')}"


def _listed(headers: list[ExhibitHeader]) -> str:
    """The listing's lines, newest first, within the count and the character bound."""
    lines: list[str] = []
    used = 0
    for header in headers[: settings.exhibit_note_max_listed]:
        line = (
            f"- {header.exhibit_id} {_quoted(header.title)} ({header.kind}, revision "
            f"{header.head_revision}, last by {header.head_author_kind})"
        )
        if used + len(line) + 1 > settings.exhibit_listing_max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if len(headers) > len(lines):
        lines.append(f"- … and {len(headers) - len(lines)} more artefact(s)")
    return "\n".join(lines)


async def resolve_exhibit_refs(
    session_id: str, refs: Sequence[ExhibitRef], *, store: ExhibitStore | None = None
) -> list[ExhibitView]:
    """The referenced revisions, each resolved within `session_id`, bound values filled in.

    Filled in (`exhibits.bindings.resolved_view`) because the note shows the model what the chemist
    is looking at, and a bound cell shows its value there, not its pointer.

    Raises:
        UnknownExhibit: a ref names an artefact this session does not hold, or a revision it does
            not have — one answer for both, as every session-scoped read gives.
    """
    found = store or default_exhibit_store()
    views: list[ExhibitView] = []
    for ref in refs:
        view = await found.view(session_id, ref.exhibit_id, ref.revision)
        if view is None:
            raise UnknownExhibit(
                f"no artefact {ref.exhibit_id!r}"
                + (f" at revision {ref.revision}" if ref.revision else "")
                + " in this session"
            )
        views.append(await resolved_view(view))
    return views


async def exhibit_turn_note(session_id: str, refs: Sequence[ExhibitRef] = ()) -> TurnNote:
    """The note to append to this turn's message: unseen chemist edits and referenced artefacts.

    Reads only; the read mark moves in `mark_told`, after the turn. Never fails the turn: an
    unreadable store is recorded as a degradation and the turn runs without the note, as it would
    on a deployment that had no artefacts.
    """
    if not settings.agent_exhibits_enabled:
        return TurnNote()
    store = default_exhibit_store()
    try:
        states = await store.states(session_id)
        notices = [
            (state.header, await _edit(store, session_id, state))
            for state in states
            if _unseen(state)
        ]
    except Exception:
        degraded(
            logger,
            "exhibits",
            "could not read session %s's artefacts for the turn note",
            session_id,
        )
        return TurnNote()
    referenced: list[ExhibitView] = []
    if refs:
        try:
            referenced = await resolve_exhibit_refs(session_id, refs, store=store)
        except UnknownExhibit:
            # Resolved at the front door a moment ago; one that has gone since is no reason to
            # fail the turn, and the chemist's own words still say what they meant.
            logger.info("an artefact referenced in session %s vanished before its turn", session_id)
    return _compose([(header, text) for header, text in notices if text], referenced)


async def mark_told(session_id: str, told: Sequence[tuple[str, int]]) -> None:
    """Record that the agent was told of each `(exhibit_id, revision)` — called after the turn.

    After, because "told" means the model saw the note: a turn torn down before its first model
    call told nobody anything. A mark that cannot be written is a degradation, not a failure; its
    cost is the same edit announced again next turn, which is the cheap direction.
    """
    if not told:
        return
    store = default_exhibit_store()
    try:
        for exhibit_id, revision in told:
            await store.mark_seen(session_id, exhibit_id, revision)
    except Exception:
        degraded(
            logger, "exhibits", "could not record which edits session %s was told of", session_id
        )


def _unseen(state: ExhibitState) -> bool:
    """Whether the head is a chemist's revision the agent has not written, read or been told of."""
    header = state.header
    return header.head_author_kind == "human" and header.head_revision > state.agent_seen_revision


async def _edit(store: ExhibitStore, session_id: str, state: ExhibitState) -> str:
    """One artefact's unseen change as text: a capped diff, or "new" for one the chemist created."""
    header = state.header
    base_revision = state.agent_seen_revision
    lead = f"{header.exhibit_id} {_quoted(header.title)} ({header.kind}): "
    if base_revision == 0:
        return lead + f"created by the chemist, now at revision {header.head_revision}."
    base = await store.view(session_id, header.exhibit_id, base_revision)
    head = await store.view(session_id, header.exhibit_id, header.head_revision)
    if base is None or head is None:  # pragma: no cover - revisions are append-only
        return ""
    full = await asyncio.to_thread(
        diff_specs, base.spec, head.spec, from_revision=base_revision, to_revision=head.revision
    )
    diff, left_out = capped(
        full,
        max_changes=settings.exhibit_diff_max_changes,
        max_chars=settings.exhibit_diff_max_value_chars,
    )
    lines = [lead + f"revision {base_revision} -> {head.revision}"]
    lines += [
        f"  {change.kind} {change.path}: {json.dumps(change.before)} -> {json.dumps(change.after)}"
        for change in diff.changes
    ]
    if left_out:
        lines.append(f"  … and {left_out} more change(s)")
    return "\n".join(lines)


def _compose(notices: list[tuple[ExhibitHeader, str]], referenced: list[ExhibitView]) -> TurnNote:
    """The note: whole edit notices that fit their share, the rest named, then the references.

    A notice is shown whole or not at all, because the read mark is per artefact: a notice cut in
    the middle would be marked told while the model saw half of it. The ones that do not fit are
    named by id (ids are minted here, so they need no framing) with the way to read them.
    """
    parts: list[str] = []
    told: list[tuple[str, int]] = []
    blocks = (1 if notices else 0) + (1 if referenced else 0)
    share = settings.exhibit_note_max_chars // max(blocks, 1)
    if notices:
        room = share - len(EDITS_HEAD) - len(frame_untrusted("", note_id="artefact-edits")) - 1
        shown: list[str] = []
        cut: list[str] = []
        for header, text in notices:
            if sum(len(t) + 1 for t in shown) + len(text) <= room:
                shown.append(text)
                told.append((header.exhibit_id, header.head_revision))
            else:
                cut.append(header.exhibit_id)
        if shown:
            framed = frame_untrusted("\n".join(shown), note_id="artefact-edits")
            parts.append(f"{EDITS_HEAD}\n{framed}")
        if cut:
            parts.append(f"{EDITS_CUT} {', '.join(cut)}")
    if referenced:
        content = "\n".join(
            json.dumps(
                {
                    "exhibit_id": view.exhibit_id,
                    "title": view.title,
                    "revision": view.revision,
                    "spec": spec_json(view.spec),
                },
                ensure_ascii=False,
            )
            for view in referenced
        )
        bounded, _ = bounded_content(content, "the artefact note", share, remedy=CUT_REMEDY)
        framed = frame_untrusted(str(bounded), note_id="artefact-refs")
        parts.append(f"{REFS_HEAD}\n{framed}")
    return TurnNote(text="\n\n".join(parts), told=tuple(told))


def _quoted(title: str) -> str:
    """A title as one JSON string: a newline or a quote in it cannot break the line it sits on."""
    return json.dumps(title, ensure_ascii=False)
