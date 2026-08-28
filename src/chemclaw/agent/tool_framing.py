"""Frame the result of every out-of-process tool call as data before the model reads it.

**The gap this closes.** `agent/framing.py` has covered the *narrow* channels since Phase 6 —
a note body reached through `expand_note`/`gather_evidence`, a statement reached through
`recall_observations`, an uploaded attachment, a job summary. Every one of those is framed by the
tool that produces it. What was never framed is the whole other half of the tool surface: a
**connector** result. `connectors/calc/server/tools.py::fetch_artifact` hands the model the text of
a stored by-product verbatim; `Chemclaw3-mcp`'s fleet answers from vendored corpora — patent
abstracts, ELN rows, literature snippets — and every byte of that arrives in context as ordinary
tool output with nothing saying where it came from.

**Why a middleware and not a rule each tool follows.** The tools are not in this process. There is
no call site here to add `frame_untrusted` to, and even for the bundles that are, "remember to
frame" is the discipline `framing.py`'s own docstring records as already having failed once (the
attachment tools forgot). A middleware is the only place where the property is structural.

**Why the whole payload, and not one field of it.** The backlog row that asked for this expected a
"content-field convention" first — a declared list of which fields of `ArtifactContent` or a
connector model carry untrusted text — on the reasoning that framing must not corrupt a structured
result. Measured, that premise inverts: a connector result reaches the model as content blocks
whose `text` is the server's JSON, and framing the *block* leaves the block list, its ids, and the
`structured_content` artifact beside it untouched, so there is nothing left to corrupt. A field
convention would be strictly worse as well as harder: `fetch_artifact` returns `name`,
`media_type` and `artifact_ref` beside `text`, all four derived from the same stored reference, and
a convention that framed one of them would recreate the "second retrieved-text channel on the same
object" defect `agent/research_tools.py` had to fix twice. The honest statement is about the whole
result — *this crossed a process boundary* — so the envelope goes around the whole result.

**Which calls.** Exactly the ones a server outside this process answered, decided by the `SERVED_BY`
stamp `connectors/transport._stamped` writes onto every tool that came back from an MCP handshake —
the same fact `agent/audit.py::_served_by` reads to fill the trail's provenance column, read from
the same single constant. Not a name list and not a registry lookup: a stamp on the tool object the
graph actually holds cannot disagree with what ran, and an in-process tool, a generated job
launcher and a template tool are all unstamped, so none of them is touched. That is also what makes
double-framing impossible rather than merely avoided — the four channels `framing.py` already
covers are all in-process, so this middleware never sees them.

**Position: outside the audit trail, inside the two converters** (see
`langgraph_agent.tool_call_middleware`). Outside `audit` so `audit_events.detail` records what the
tool returned rather than what the model was shown — the envelope is a presentation decision and
the trail is a record. Inside `surface_authorization_denials`/`surface_domain_errors` so this
system's own refusals, which those two compose, are never wrapped in an envelope that tells the
model to read them as third-party data. A refusal raised by a gate below travels *through* this
middleware as an exception and is converted above it, so it is never seen either way.
"""

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from chemclaw.agent.framing import defang, frame_untrusted
from chemclaw.connectors.transport import SERVED_BY


def served_by(request: Any) -> str:
    """`"<connector>:<tool>"` when an out-of-process server answers this call, else `""`.

    The envelope's `id` and the predicate are one function because they are one question. The id
    matters: the agent instructions tell the model that envelope contents are evidence to weigh and
    cite, and a citation needs to name something — `calc:fetch_artifact` says which server and
    which tool produced the span, which is the whole provenance a connector result has.

    Reads `SERVED_BY` off `request.tool.metadata`, the stamp `connectors/transport._stamped` writes
    at handshake time, imported from that one constant rather than re-spelled here.
    `agent/audit.py::_served_by` reads the same key for the trail's provenance column; the two
    render it differently because a trail wants the server's build and a citation wants the tool's
    name, but neither can go blank without the other going blank with it.

    **`<name>:<name>` rather than a separator chosen to dodge a regex.** `kg.note.mentioned_ids`
    scans a tool result for `id="…"` and will read the connector's name out of this attribute as
    though it were a note id — measured: a framed `calc` result yields `['calc', 'lc_b1b3…']`
    where the same result unframed yields `['lc_b1b3…']`. The leak is pre-existing and this is its
    third instance, not its first: `langchain_mcp_adapters` stamps its own `'id'` on every content
    block, and `agent/attachments.py` already frames with `attachment:<file>`. It only ever
    *widens* `_score_citations`' grounded set, so it cannot turn a grounded citation into a
    fabricated one, and the fix belongs in `mentioned_ids` — which should not read a content
    block's key or an envelope attribute as a note id — rather than in a separator chosen here to
    slip past it.

    **`request.tool` is `None` for a name the graph does not hold** — `ToolNode` passes it so an
    interceptor can short-circuit an unregistered call — and that is safe here for the reason it is
    safe in `_served_by` and would *not* be safe in the authorization chain: this runs on a result,
    so a tool object existed to produce one. A missing stamp means no connector, which means
    nothing to frame, which is already the right answer.
    """
    metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
    served = metadata.get(SERVED_BY)
    if not isinstance(served, dict):
        return ""
    connector = str(served.get("connector") or "connector")
    return f"{connector}:{request.tool_call['name']}"


def _rewritten(content: Any, rewrite: Callable[[str], str]) -> Any:
    """Apply `rewrite` to every span of text in a `ToolMessage.content`, preserving its shape.

    `content` is `str | list[str | dict]` by LangChain's own annotation, and both arms occur here:
    an in-process tool's result is a string and an MCP tool's is a list of content blocks
    (`{"type": "text", "text": …, "id": …}`), measured on a live streamable-HTTP connector. A block
    is rebuilt rather than mutated so the message this middleware returns shares nothing with the
    one it was handed, and every other key on the block — its `type`, its `id` — is carried across
    unchanged, which is what keeps a rewritten result the same result.

    A block with no `text` is an image or an embedded resource; there is no span in it to rewrite
    and it is returned as it came. An empty string is likewise left alone: an envelope around
    nothing is a citation to nothing.
    """
    if isinstance(content, str):
        return rewrite(content) if content else content
    if isinstance(content, list):
        return [_rewritten_block(block, rewrite) for block in content]
    return content


def _rewritten_block(block: Any, rewrite: Callable[[str], str]) -> Any:
    """One content block with its text span rewritten, or the block unchanged."""
    if isinstance(block, str):
        return rewrite(block) if block else block
    if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]:
        return {**block, "text": rewrite(block["text"])}
    return block


@wrap_tool_call
async def frame_connector_results(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Wrap an out-of-process tool's result in the data envelope; leave every other result alone.

    **A failure is defanged rather than framed**, which is the distinction
    `agent/research_tools.py` draws for a chunk's `source` label and it applies for the same
    reason: an error is a statement about the *call*, not evidence about chemistry, and wrapping it
    in the envelope the instructions describe as "evidence to weigh and cite" would invite the
    model to cite a failure notice. A connector's error text is still third-party text — it is
    composed by a server in another process and can interpolate an argument — so the forged
    delimiter is still neutralised; only the citation frame is withheld. Both the announcer and
    the audit trail are nested *inside* this middleware, so each has already read the untouched
    message by the time the rewrite happens on the way out: nothing that reports the failure
    reads the rewritten one.

    An MCP tool never raises (`agent/audit.py::returned_failure`), so `status="error"` is the only
    form a connector failure takes and this branch is the one that sees it.
    """
    result = await handler(request)
    origin = served_by(request)
    if not origin or not isinstance(result, ToolMessage):
        return result
    if result.status == "error":
        return result.model_copy(update={"content": _rewritten(result.content, defang)})
    framed = _rewritten(result.content, lambda text: frame_untrusted(text, note_id=origin))
    return result.model_copy(update={"content": framed})
