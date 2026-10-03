"""The agent's three artefact tools: create one, revise one, read one back.

An artefact is **part of the answer, not an effect**
(`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`): it changes nothing in a
laboratory, the knowledge graph or another system, so these tools are read-only for authorization
(`agent/authz.READ_ONLY_TOOLS`) and need no approved plan. The two that write announce the write on
the chemist's stream (`record_exhibit`), which is exactly why they are kept off every helper
(`agent/subagents.SPEAKS_TO_THE_CHEMIST`): a helper creating one would put something on the
chemist's screen from a context the chemist cannot see.

**The spec is an untyped object at this boundary and validated behind it.** The decision record
measured a typed union at 1,989 prefix tokens for the three tools against 961 for this shape, with
the typed create tool alone over `MAX_SINGLE_TOOL_TOKENS`. What that gives up is constrained
generation; what replaces it is `exhibits.models.parse_spec`, which refuses a malformed spec with a
worded error the model can correct in one retry.

**What the model is told is defanged, never framed.** `read_exhibit` returns text a chemist may have
typed and text an earlier turn wrote, and a stored delimiter would otherwise be replayed into every
later read — `protocol_design_tools._readable`'s argument, for the same kind of document.

**Only the turn's own session is reachable.** Every call resolves the artefact against the ambient
session id, so an id from another conversation answers as an unknown one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from chemclaw.agent.authz import require_actor
from chemclaw.agent.framing import defang
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import ExhibitSignal, record_exhibit
from chemclaw.exhibits.bindings import bind_for_write, resolved_view
from chemclaw.exhibits.diff import capped, diff_specs
from chemclaw.exhibits.grounding import unverified_figures
from chemclaw.exhibits.models import (
    DocumentSpec,
    ExhibitView,
    Spec,
    StaleRevision,
    UnknownExhibit,
    parse_spec,
    require_creatable,
    require_writable,
    spec_json,
)
from chemclaw.exhibits.sources import require_source_stored, spec_for_model
from chemclaw.exhibits.store import ExhibitStore, default_exhibit_store
from chemclaw.exhibits.telemetry import record_write, refusals_counted

#: Kinds only a chemist creates. A `result` artefact pins a stored tool result by its content hash,
#: which the UI holds and the model never sees, so a model-written one would name a ref it guessed.
_CHEMIST_ONLY_KINDS = frozenset({"result"})


class Edit(BaseModel):
    """One exact replacement in a document: `old`, which must occur once, becomes `new`.

    The tool takes `edits` as plain objects and validates them here, rather than publishing this
    model in its schema: a nested model costs its JSON schema on every model call, and the prefix
    this feature may spend is bounded (see the module docstring).
    """

    old: str
    new: str

    model_config = ConfigDict(extra="forbid")


_EDITS: TypeAdapter[list[Edit]] = TypeAdapter(list[Edit])


#: The part of `create_exhibit`'s description that names the html kind — what a deployment with
#: `agent_html_artefacts_enabled` off removes, so the model is not offered a kind it is refused.
HTML_CLAUSE = "; html `html` (self-contained, no network), `height`"


def described_for_deployment(tool: BaseTool) -> BaseTool:
    """`tool`, or a copy of `create_exhibit` without the html clause when html artefacts are off.

    A copy per build rather than a second registered function: the schema stays the one
    `tool_schema.as_structured_tool` derived once, and only the description text differs. The
    clause is required to be in the description, so an edit to the docstring that drops or rewords
    it fails here rather than leaving html advertised on a deployment that refuses it.
    """
    if tool.name != "create_exhibit" or settings.agent_html_artefacts_enabled:
        return tool
    if HTML_CLAUSE not in tool.description:
        raise RuntimeError("create_exhibit's description no longer carries HTML_CLAUSE verbatim")
    return tool.model_copy(update={"description": tool.description.replace(HTML_CLAUSE, "", 1)})


def _session() -> str:
    """The turn's session, or a refusal: an artefact always belongs to one conversation."""
    session_id = get_current_session_id()
    if not session_id:
        raise ChemclawError("artefacts belong to a conversation, and this call has none")
    return session_id


def _store() -> ExhibitStore:
    """The deployment's artefact store, resolved per call.

    Per call rather than bound at import, because the backend follows `session_store` and a tool
    module is imported once for the life of the process — and so that a test patching this one
    name reaches every tool here.
    """
    return default_exhibit_store()


@tool
async def create_exhibit(title: str, spec: dict[str, Any]) -> str:
    """Show the chemist a plan, report, table (4+ rows), structures or chart beside your answer.

    Revise it later rather than copying it. Figures no tool returned are flagged unchecked.
    `spec` fields by `kind`: document `markdown`; table `columns` [{key,label,unit}], `rows`;
    structures `items` [{smiles,label,props}]; chart `chart` (line|scatter|bar), `x_label`,
    `y_label`, `series` [{name,x,y}]; geometry `structure_id` (from a result), `label`; link
    `target` (protocol|note|job), `id`; html `html` (self-contained, no network), `height`.
    Copy a value from a result ending ⟨r:HEX⟩: any cell, prop, smiles, x or y may be
    {"$bind":{"result":"r:HEX","pointer":"/json/pointer"}}; a table may give `rows_from`
    {result,pointer,columns:{key:pointer}} for rows.

    Returns:
        JSON with `exhibit_id` and `revision`.
    """
    session_id = _session()
    with refusals_counted():
        parsed = parse_spec(spec)
        if parsed.kind in _CHEMIST_ONLY_KINDS:
            raise ChemclawError(
                "a 'result' artefact is pinned by the chemist from a tool result block; show the "
                "values as a table, or say which result to pin"
            )
        require_creatable(parsed)
        bound = await bind_for_write(session_id, parsed)
        require_writable(bound.resolved, title=title, change_note="", stored=bound.stored)
        await require_source_stored(parsed, session_id)
        view = await _store().create(
            session_id,
            title=title,
            spec=bound.stored,
            author_kind="agent",
            author=require_actor(),
            correlation_id=get_current_correlation_id() or "",
            unverified_figures=await unverified_figures(session_id, bound.stored),
        )
    record_exhibit(_announced(view, "created"))
    record_write(view, "created")
    return json.dumps({"exhibit_id": view.exhibit_id, "revision": view.revision})


@tool
async def revise_exhibit(
    exhibit_id: str,
    base_revision: int,
    note: str,
    edits: list[dict[str, str]] | None = None,
    spec: dict[str, Any] | None = None,
) -> str:
    """Change an artefact as a new revision, with a one-line `note` on what changed and why.

    Pass `edits` [{old, new}] (a document; each `old` occurs once) or a whole new `spec`. Refused
    if `base_revision` is not the latest: the chemist edited it, so `read_exhibit` first and keep
    their change.

    Returns:
        JSON with the new `revision`.
    """
    session_id = _session()
    store = _store()
    try:
        with refusals_counted():
            current = await store.view(session_id, exhibit_id)
            if current is None:
                raise UnknownExhibit(f"no artefact {exhibit_id!r} in this conversation")
            if current.kind in _CHEMIST_ONLY_KINDS:
                raise ChemclawError(
                    f"{exhibit_id} is a result the chemist pinned; it is not revisable"
                )
            if base_revision != current.head_revision:
                raise StaleRevision(exhibit_id, current.head_revision, base_revision)
            revised = _revised_spec(current, edits, spec)
            bound = await bind_for_write(session_id, revised, parent=current.raw_spec)
            require_writable(
                bound.resolved,
                title=current.title,
                change_note=note,
                stored=bound.stored,
                vanished=bound.vanished,
            )
            await require_source_stored(revised, session_id, parent=current.raw_spec)
            chemist = await store.chemist_figures(session_id, exhibit_id)
            view = await store.append(
                session_id,
                exhibit_id,
                spec=bound.stored,
                parent_revision=base_revision,
                author_kind="agent",
                author=require_actor(),
                change_note=note,
                correlation_id=get_current_correlation_id() or "",
                unverified_figures=await unverified_figures(
                    session_id, bound.stored, chemist_figures=chemist
                ),
            )
    except StaleRevision as exc:
        raise ChemclawError(_stale(exhibit_id, exc.head, base_revision)) from exc
    record_exhibit(_announced(view, "revised"))
    record_write(view, "revised")
    return json.dumps({"exhibit_id": exhibit_id, "revision": view.revision})


@tool
async def read_exhibit(exhibit_id: str, revision: int = 0) -> str:
    """Read an artefact (`revision` 0 = latest) and the chemist's changes since your last one.

    Returns:
        JSON with `spec` (bound values filled in), `raw_spec` and `bindings` when it binds any,
        and `changes_since_agent`.
    """
    session_id = _session()
    store = _store()
    view = await store.view(session_id, exhibit_id, revision)
    if view is None:
        raise ChemclawError(
            f"no artefact {exhibit_id!r}"
            + (f" at revision {revision}" if revision else "")
            + " in this conversation"
        )
    shown = await resolved_view(view)
    readout: dict[str, Any] = {
        "exhibit_id": exhibit_id,
        "kind": view.kind,
        "title": view.title,
        "revision": view.revision,
        "head_revision": view.head_revision,
        "author_kind": view.author_kind,
        # A cited structure is shown as its address (`sources.spec_for_model`).
        "spec": spec_json(spec_for_model(shown)),
    }
    if shown.bindings:
        # Both forms, because a revision is written in the stored one: a `spec` sent back with the
        # values filled in would detach every binding the chemist can see the provenance of.
        readout["raw_spec"] = spec_json(view.raw_spec)
        readout["bindings"] = [binding.model_dump(mode="json") for binding in shown.bindings]
    readout["unchecked_figures"] = view.unverified_figures
    readout["changes_since_agent"] = await _changes_since_agent(store, session_id, view)
    # No read mark is set here, deliberately: a helper holds this tool too, and a helper reading
    # the chemist's edit is not the agent that answers the chemist having seen it. The mark moves
    # on the agent's own writes and when the turn note announces an edit (`agent/exhibit_notes`).
    return defang(json.dumps(readout, ensure_ascii=False))


async def _changes_since_agent(
    store: ExhibitStore, session_id: str, view: ExhibitView
) -> dict[str, Any] | None:
    """The chemist's changes between the agent's last revision and `view`, capped — or `None`.

    `None` when there is nothing to compare: the agent wrote `view` itself, or never wrote this
    artefact at all (a result the chemist pinned, a table they started).
    """
    history = await store.revisions(session_id, view.exhibit_id) or []
    agent_revisions = [
        entry.revision
        for entry in history
        if entry.author_kind == "agent" and entry.revision <= view.revision
    ]
    if not agent_revisions or agent_revisions[-1] == view.revision:
        return None
    base = await store.view(session_id, view.exhibit_id, agent_revisions[-1])
    if base is None:  # pragma: no cover - revisions are append-only and were just listed
        return None
    full = await asyncio.to_thread(
        diff_specs, base.spec, view.spec, from_revision=base.revision, to_revision=view.revision
    )
    diff, left_out = capped(
        full,
        max_changes=settings.exhibit_diff_max_changes,
        max_chars=settings.exhibit_diff_max_value_chars,
    )
    shown = diff.model_dump(mode="json")
    if left_out:
        shown["more_changes"] = left_out
    return shown


def _revised_spec(
    current: ExhibitView, edits: list[dict[str, str]] | None, spec: dict[str, Any] | None
) -> Spec:
    """The spec a revision writes: a whole new one, or the document with `edits` applied.

    Raises:
        ChemclawError: neither or both were given, edits were given for something not a document,
            or an `old` does not occur exactly once in the text it is applied to.
    """
    if (edits is None) == (spec is None):
        raise ChemclawError("pass exactly one of `edits` (a document) or `spec` (a whole new spec)")
    if spec is not None:
        return parse_spec(spec)
    if not isinstance(current.spec, DocumentSpec):
        raise ChemclawError(
            f"`edits` applies to a document; {current.exhibit_id} is a {current.kind} — send `spec`"
        )
    try:
        replacements = _EDITS.validate_python(edits or [])
    except ValidationError as exc:
        raise ChemclawError(f"each edit is {{old, new}}: {exc.errors()[0]['msg']}") from exc
    markdown = current.spec.markdown
    for index, edit in enumerate(replacements):
        found = markdown.count(edit.old) if edit.old else 0
        if found != 1:
            raise ChemclawError(
                f"edits[{index}].old occurs {found} times in revision {current.revision}; it must "
                "occur exactly once — quote more of the surrounding text"
            )
        markdown = markdown.replace(edit.old, edit.new, 1)
    return DocumentSpec(kind="document", markdown=markdown)


def _stale(exhibit_id: str, head: int, base: int) -> str:
    """The refusal a stale base gets — the instruction that keeps the chemist's edit."""
    return (
        f"{exhibit_id} is at revision {head}, not {base}: the chemist revised it. Call "
        "read_exhibit first and keep their change in your revision."
    )


def _announced(view: ExhibitView, op: Literal["created", "revised"]) -> ExhibitSignal:
    """The stream announcement for a write the agent just made."""
    return ExhibitSignal(
        exhibit_id=view.exhibit_id,
        revision=view.revision,
        kind=view.kind,
        title=view.title,
        op=op,
        author_kind=view.author_kind,
        author=view.author,
    )
