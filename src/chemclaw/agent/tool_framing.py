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

**Which calls, and what each one gets.** This middleware decides *three* treatments, not two, and
saying "only out-of-process results are touched" was this docstring's own claim for as long as that
was false — it went stale when the `task` branch landed and doubly stale when the scratchpad verbs
joined it. The three:

1. **Framed** — exactly the calls a server outside this process answered, decided by the `SERVED_BY`
   stamp `connectors/transport._stamped` writes onto every tool that came back from an MCP
   handshake, the same fact `agent/audit.py::_served_by` reads to fill the trail's provenance
   column, read from the same single constant. Not a name list and not a registry lookup: a stamp on
   the tool object the graph actually holds cannot disagree with what ran. That is also what makes
   double-framing impossible rather than merely avoided — the four channels `framing.py` already
   covers are all in-process, so this middleware never sees them there.
2. **Defanged and not framed** — a connector *failure*, a helper's report (`subagent_tool_names()`)
   and every scratchpad verb (`scratchpad_tools()`). Each carries text that can spell the closing
   delimiter and none of them is evidence a citation may name; `frame_connector_results`' own
   docstring argues each case.
3. **Left alone** — everything else. An in-process tool that frames its own spans, a generated job
   launcher, a template tool: unstamped, not a helper, not a file verb, and nothing to rewrite.

The two rewritten sets are read from the functions that *derive* them rather than from a list
spelled here, which is the same argument `subagents.helper_profile` makes for
`authz.side_effecting_tools()`: a verb an upstream bump adds is covered the day it is bound, and the
two verbs this deployment withholds — `execute` and `delete` — never enter the set, because
`scratchpad_tools()` is where they are withheld.

**The stamp is asked first, and a name is asked only of what the stamp did not claim.** The order
is load-bearing rather than stylistic, because the two sets overlap on names a *connector* may
plausibly declare. `_declared_tool_names` refuses one bundle's name colliding with another's; it
does not compare against the ambient names, so a connector declaring `read_file` — which a code
execution or document server would reasonably do — is not refused. Measured against a live
streamable-HTTP server declaring one: it wins `ToolNode.tools_by_name` **and** carries the
`SERVED_BY` stamp. Asking the name first would therefore defang a genuinely third-party payload
instead of framing it, stripping the envelope and the `probe:read_file` provenance from content
that crossed a process boundary — exactly backwards, and a regression introduced by widening the
name set from one to seven. A stamped tool ran outside this process whatever it is called, so the
stamp decides and the names only sort what is left.

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

from chemclaw.agent.framing import defang, envelope_delimiters, frame_untrusted
from chemclaw.agent.tool_result_shape import rewritten_tool_messages
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
    if not _carries_text(block):
        return block
    if isinstance(block, str):
        return rewrite(block)
    return {**block, "text": rewrite(block["text"])}


def _carries_text(block: Any) -> bool:
    """Whether this block has a non-empty text span — i.e. whether there is anything to rewrite.

    One predicate rather than a condition spelled twice, because `_framed_content` has to ask the
    same question a second time: it needs to know which blocks the envelope's two halves ride on,
    and that is exactly the set `_rewritten_block` would touch. A block with no span is an image or
    an embedded resource; an empty one is a citation to nothing.
    """
    if isinstance(block, str):
        return bool(block)
    return isinstance(block, dict) and isinstance(block.get("text"), str) and bool(block["text"])


def _framed_content(content: Any, origin: str) -> Any:
    """`content` inside **one** envelope naming `origin`, whatever shape it arrived in.

    **One result, one envelope — which is what this module's docstring has always argued for and
    what the code did not do.** A connector result is `str | list[block]`, and the list arm framed
    *each* block: the envelope is a constant per block (~90 characters for a short connector name)
    and the block count is bounded by nothing, so what the model read was `text + 90 x blocks`
    while `agent/tool_result_size.bound_tool_results` — nested inside this middleware — had
    measured and capped only the text. Measured: 20,000 blocks of 2 characters is 40,000
    characters, comfortably under the 60,000-character ceiling so nothing was cut, and the result
    reached the model at **1,840,000** characters, 46x its measured size and over four times the
    whole configured request budget.

    Framing the result once removes that term rather than accounting for it, and it loses nothing:
    the id repeated on every block was the same id every time, and the provider renders a block
    list in sequence, so an envelope opened on the first span and closed on the last is one
    envelope to the model as well as to `kg.note.mentioned_ids`.

    **Every span is still defanged, and that is what makes one envelope safe.** The two delimiters
    ride on the first and last text spans, so a *middle* span able to spell the closing delimiter
    would end the envelope early and put everything after it outside the frame — which is the
    forgery the envelope exists to close, arriving through the shape rather than through the
    content. Neutralisation is per span; only the citation frame is shared.
    """
    if isinstance(content, str):
        return frame_untrusted(content, note_id=origin) if content else content
    if not isinstance(content, list):
        return content
    spans = [index for index, block in enumerate(content) if _carries_text(block)]
    if not spans:
        # Nothing to frame, and an envelope around nothing is a citation to nothing.
        return content
    opening, closing = envelope_delimiters(origin)
    first, last = spans[0], spans[-1]

    def _rewrite(index: int) -> Callable[[str], str]:
        head = opening if index == first else ""
        tail = closing if index == last else ""
        return lambda text: f"{head}{defang(text)}{tail}"

    return [_rewritten_block(block, _rewrite(index)) for index, block in enumerate(content)]


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

    **A helper's report is defanged on the same grounds, and it is the third case rather than an
    exception to the first two** (`D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-
    thread`). What `task` returns is prose a model wrote after reading evidence that arrived framed,
    and it lands in the caller's thread as an ordinary tool result — measured, carrying a **live**
    closing delimiter, because nothing on that path rewrote it. Every other route by which model
    prose or third-party text reaches a prompt neutralises it: `agent/condense.py` defangs each
    field the digest model returns, `agent/verifier.py` defangs the answer under review, and
    retrieved content is framed at its source. This was the one span that arrived raw.

    The nonce does not cover this the way it covers external content. `frame_untrusted`'s own
    docstring is explicit that "forgery is closed by *defanging* the content, and the nonce and the
    defang each cover the other's gap" — and a helper is inside the deployment, so it does not have
    to *guess* the tag: it has just read it, in the envelopes around its own evidence. A report that
    reproduces the delimiter puts everything after it outside an envelope as far as the caller's
    model can tell, which is exactly the laundering
    `D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool` declines a summarizer
    for.

    **Defanged and not framed**, for the reason the error branch above is: an envelope tells the
    model the span is evidence to weigh and cite, and a helper's summary is neither — citing it
    would credit a source that is this system's own paraphrase. The delimiter is neutralised; the
    citation frame is withheld. What the caller still cannot see is *that* the report is derived
    from untrusted reading, which is an epistemic gap rather than a mechanical one and is a
    `docs/planning/BACKLOG.md` row.

    **A scratchpad read is the fourth case and it takes the same treatment, for the same reason**
    (`D-2026-09-04-a-helpers-file-crosses-back-and-stays`). The report is not the only thing that
    crosses a helper's boundary: `deepagents`' `_EXCLUDED_STATE_KEYS` is `{"messages", "todos",
    "structured_response"}`, and `files` is not among them, so a helper's `/scratch/evidence.md`
    lands in its caller's `files` channel — deliberately kept, because pointer-passing is better
    context economics than pasting the reading into a report. What crossed still has to be read
    back, and `read_file` is in-process: `served_by(request)` returns `""` for it, so before this
    branch the caller's read arrived with **nothing applied** — byte for byte the file the helper
    wrote, a copied `</retrieved-note-…>` **live** in it, plus `read_file`'s own line prefix and
    not one character else. That relation, rather than a character count, is what
    `tests/test_subagents.py::test_a_helpers_file_reaches_its_caller_and_is_defanged_when_read`
    asserts: a count would be a claim about this fixture's wording as much as about the code
    (`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`).

    **Defanged rather than framed**, and here the argument is sharper than it is for a report:
    `/scratch/` is this system's own notepad, so an envelope around a file the turn wrote itself
    would invite a citation crediting the system for its own prose — the same distinction the error
    branch above draws, one channel further in.

    **The coverage is the whole verb set, not `read_file`.** Three channels carry the text and only
    one of them is a file read. `grep(output_mode="content")` returns matching lines, so it is a
    second content channel — measured live at 121 characters with the same live delimiter. And
    `write_file` **echoes the path** in its confirmation, so
    `write_file(file_path="/scratch/</retrieved-note-…>.md")` puts a live delimiter in the caller's
    thread with no helper and no read involved at all — measured at 59 characters. Keying on
    `scratchpad_tools()` rather than on a name this docstring picked is what makes those three one
    case instead of three fixes.

    **A verb is not a root, so this also covers `/skills/` and `/memories/`, and the answer is the
    same for all three.** `scratchpad_backend` routes one `read_file` to three places; none of them
    is a citable source. A `SKILL.md` is this repository's own judgment, checked into git behind the
    role gate — the model is told to load it and act on it, not to cite it. A memory is prose the
    model wrote itself on an earlier turn, which is the helper's report one remove further out.
    Framing either would be the same misattribution as framing a scratch note.

    **What is stored and what the model sees now differ, and that has one consequence worth
    naming.** Nothing on disk or in the `files` channel is rewritten — this is a presentation
    decision taken on the way out, so the stored content stays pristine and a later read starts
    from the same bytes. But a model that copies a string *out of what it just read* and hands it
    back as `edit_file`'s `old_string` is copying the escaped form while the file holds the live
    one, and the edit comes back `Error: String not found in file`. Narrow while only a delimiter
    is escaped. It widens on `framing._defang`'s second pass, which escapes **every** `<` in the
    content once an invisible character reveals a disguised tag: one zero-width byte anywhere in a
    scratch file and that file's read→edit loop breaks for all of its markup rather than only for
    the tag. The recovery is the one a model already has — edit on a span it did not copy through a
    rewrite — and the alternative, a live delimiter in the caller's thread, is what this branch
    exists to stop.
    """
    # Imported here rather than at module scope: `chemclaw_agent` reaches this module's siblings
    # through the agent builder, and both sets are properties of the installed package — each
    # `@cache`d where it is derived, so a call here costs a hash lookup in a frozenset and a scan of
    # a six-name tuple, rather than building two middlewares to ask them.
    from chemclaw.agent.chemclaw_agent import subagent_tool_names
    from chemclaw.agent.scratchpad import scratchpad_tools

    result = await handler(request)

    def _defanged(message: ToolMessage) -> ToolMessage:
        return message.model_copy(update={"content": _rewritten(message.content, defang)})

    origin = served_by(request)
    if origin:

        def _framed(message: ToolMessage) -> ToolMessage:
            if message.status == "error":
                return _defanged(message)
            return message.model_copy(update={"content": _framed_content(message.content, origin)})

        return rewritten_tool_messages(result, _framed)
    name = request.tool_call["name"]
    if name in subagent_tool_names() or name in scratchpad_tools():
        return rewritten_tool_messages(result, _defanged)
    return result
