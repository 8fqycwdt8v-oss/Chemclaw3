"""The per-turn artefact note: what artefacts exist, what the chemist changed, what they point at.

Appended to the turn's own message at turn start (`api/runner.run_turn`), beside the job push-back
`_with_pushed_job_results` appends there — **not** to the system message on every call, which is
how `agent/preferences.StandingPreferences` reaches the model, and the difference is the point:

- A chemist's edit is an *event*, to be told once. Its notice goes into the thread with the turn
  that follows it, and the agent's read mark (`session_exhibits.agent_seen_revision`) moves as it is
  delivered, so the next turn does not repeat it. A system-message section is rebuilt on every
  call and has nowhere to keep "already told".
- The listing of what exists rides with it, so no list tool is needed (the decision record kept the
  surface at three schemas). It is written only on a turn where at least one artefact exists, and
  it is short: ids, titles, kinds and head revisions, bounded by `exhibit_note_max_listed`.
- Artefacts the chemist referenced in this message (`MessageIn.exhibit_refs`) are copied in, so
  "make the second column percent" reaches the model with the table it is about.

**Framed as data, not instruction.** A chemist's edit and a referenced artefact are text being
shown to the model, the same discipline every retrieved note and job result has
(`agent/framing.frame_untrusted`); titles in the listing sit outside an envelope and are defanged
and flattened to one line. The whole note is bounded by `exhibit_note_max_chars`, because this is a
`HumanMessage` producer and
`D-2026-09-16-a-mailbox-nobody-bounded-is-a-human-message-nobody-bounded`
is the cost of one that is not.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from chemclaw.agent.framing import defang, frame_untrusted
from chemclaw.agent.tool_result_size import bounded_content
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.model_prose import ModelProse
from chemclaw.exhibits.diff import capped, diff_specs
from chemclaw.exhibits.models import (
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
    "revise_exhibit rather than creating a copy):"
)
EDITS_HEAD = ModelProse(
    "Since you last wrote or were told about them, the chemist changed these artefacts. The "
    "changes follow as data; keep them in any revision you make, and read_exhibit for the whole "
    "artefact."
)
REFS_HEAD = ModelProse(
    "The chemist's message refers to these artefacts; each follows as data, at the revision they "
    "referred to."
)
CUT_REMEDY = ModelProse("call read_exhibit for the whole artefact")


async def resolve_exhibit_refs(
    session_id: str, refs: Sequence[ExhibitRef], *, store: ExhibitStore | None = None
) -> list[ExhibitView]:
    """The referenced revisions, each resolved within `session_id`.

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
        views.append(view)
    return views


async def exhibit_turn_note(session_id: str, refs: Sequence[ExhibitRef] = ()) -> str:
    """The note to append to this turn's message, or `""` when there is nothing to say.

    Never fails the turn: an unreadable store is recorded as a degradation and the turn runs
    without the note, as it would on a deployment that had no artefacts.
    """
    if not settings.agent_exhibits_enabled:
        return ""
    store = default_exhibit_store()
    try:
        states = await store.states(session_id)
        edits = [await _edit(store, session_id, state) for state in states if _unseen(state)]
    except Exception:
        degraded(
            logger,
            "exhibits",
            "could not read session %s's artefacts for the turn note",
            session_id,
        )
        return ""
    referenced: list[ExhibitView] = []
    if refs:
        try:
            referenced = await resolve_exhibit_refs(session_id, refs, store=store)
        except UnknownExhibit:
            # Resolved at the front door a moment ago; one that has gone since is no reason to
            # fail the turn, and the chemist's own words still say what they meant.
            logger.info("an artefact referenced in session %s vanished before its turn", session_id)
    if not states and not referenced:
        return ""
    note = _compose(states, [edit for edit in edits if edit], referenced)
    try:
        for state in states:
            if _unseen(state):
                await store.mark_seen(
                    session_id, state.header.exhibit_id, state.header.head_revision
                )
    except Exception:
        # The note still goes out; the cost of a mark that did not move is the same edit announced
        # again next turn, which is the cheap direction.
        degraded(
            logger, "exhibits", "could not record which edits session %s was told of", session_id
        )
    return note


def _unseen(state: ExhibitState) -> bool:
    """Whether the head is a chemist's revision the agent has not written, read or been told of."""
    header = state.header
    return header.head_author_kind == "human" and header.head_revision > state.agent_seen_revision


async def _edit(store: ExhibitStore, session_id: str, state: ExhibitState) -> str:
    """One artefact's unseen change as text: a capped diff, or "new" for one the chemist created."""
    header = state.header
    base_revision = state.agent_seen_revision
    lead = f'{header.exhibit_id} "{_flat(header.title)}" ({header.kind}): '
    if base_revision == 0:
        return lead + f"created by the chemist, now at revision {header.head_revision}."
    base = await store.view(session_id, header.exhibit_id, base_revision)
    head = await store.view(session_id, header.exhibit_id, header.head_revision)
    if base is None or head is None:  # pragma: no cover - revisions are append-only
        return ""
    diff, left_out = capped(
        diff_specs(base.spec, head.spec, from_revision=base_revision, to_revision=head.revision),
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


def _compose(states: list[ExhibitState], edits: list[str], referenced: list[ExhibitView]) -> str:
    """The note itself: the listing, then each framed block bounded to its share of the budget."""
    shown = states[: settings.exhibit_note_max_listed]
    listing: list[str] = [LISTING_HEAD]
    listing += [
        f'- {s.header.exhibit_id} "{_flat(s.header.title)}" ({s.header.kind}, revision '
        f"{s.header.head_revision}, last by {s.header.head_author_kind})"
        for s in shown
    ]
    if len(states) > len(shown):
        listing.append(f"- … and {len(states) - len(shown)} older artefact(s)")
    blocks: list[tuple[str, str, str]] = []
    if edits:
        blocks.append((EDITS_HEAD, "\n".join(edits), "artefact-edits"))
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
        blocks.append((REFS_HEAD, content, "artefact-refs"))
    head = "\n".join(listing) if states else ""
    share = max((settings.exhibit_note_max_chars - len(head)) // max(len(blocks), 1), 1)
    parts = [head] if head else []
    for lead, content, source in blocks:
        bounded, _ = bounded_content(content, "the artefact note", share, remedy=CUT_REMEDY)
        parts.append(f"{lead}\n{frame_untrusted(str(bounded), note_id=source)}")
    return "\n\n".join(parts)


def _flat(text: str) -> str:
    """A title as one defanged line, for the parts of the note outside an envelope."""
    return defang(" ".join(text.split()))
