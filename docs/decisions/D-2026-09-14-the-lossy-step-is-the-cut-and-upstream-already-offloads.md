# D-2026-09-14-the-lossy-step-is-the-cut-and-upstream-already-offloads — what "make a cleared tool result retrievable" actually has to change, measured before it is written

## Status

Accepted as a measurement and a reframing. The build it scopes is **deliberately not in this
commit** — see *Decision*.

## Context

The 2026-09-13 capability audit scheduled a Wave 1 item: *"make a cleared tool result retrievable by
address"*. Its premise was that context compaction throws a result away and the model has no way
back to it. Four things were measured before writing any of it, and three of them change what the
change should be.

## What the measurement found

**1. The clear is not the lossy step; the cut is.** `ClearOlderToolResultsEdit` runs inside
`wrap_model_call` over a deep copy, so it narrows the list *this model call* is sent and leaves graph
state untouched — `agent/compaction.py` says so in as many words, and `_publish_reduction` proves it
by comparing `request.state["messages"]` against `request.messages`. The next turn re-derives the
same reduction from the full thread. What is genuinely destroyed is `agent/tool_result_size.py`'s
head-and-tail cut: `bound_tool_results` is a `wrap_tool_call` middleware, so the message `ToolNode`
appends to state, the checkpointer persists and `session_messages` stores is the **already cut** one.
The item's title names the half that loses nothing.

**2. Upstream already implements the feature, and this repository's ceiling makes it dead code.**
`FilesystemMiddleware.awrap_tool_call` → `_aintercept_large_tool_result` →
`_aoffload_tool_message_content` writes an oversized result to
`{artifacts_root}/large_tool_results/{tool_call_id}` and replaces the message with a notice telling
the model to `read_file` it with `offset` and `limit`. It fires at `tool_token_limit_before_evict`
× 4 = 80,000 characters; `agent_max_tool_result_chars` is 60,000 and `bound_tool_results` sits
*inside* it, so it never fires. That suppression is not accidental —
`tests/test_tool_framing.py::test_a_connector_success_survives_the_ceiling_instead_of_being_evicted`
exists to hold it, and the reason is good: a cut keeps **both ends** of the result, an eviction
replaces the whole thing with a pointer, and a model that can still read the head and the tail often
needs no second call at all. Two further facts decide any adoption: this repository's backend has
`artifacts_root == "/"`, so upstream's path is `/large_tool_results/<id>` — outside both
`/scratch/**` and `/memories/**` — and its wording is what that test asserts the absence of.

**3. The shape a first-party offload needs works, end to end.** Driven on a really compiled graph
with the probe middleware substituted into `bound_tool_results`' own slot: a `wrap_tool_call`
middleware may return `Command(update={"messages": [msg], "files": {path: FileData}})`, the tool call
is answered normally, `files` carries the content intact, and a **separately compiled graph on the
same thread** reads it back identically through the real Postgres checkpointer. `read_file` takes a
0-based `offset` and a `limit`, returns line-numbered text with a pagination footer, and
`filesystem_permissions()` does not gate it — every rule there declares `operations=["write"]`, so
reads are ungated everywhere. One trap worth recording: such a middleware **must be async**. A sync
one raises `NotImplementedError` from LangChain and the failure is swallowed into a generic
"that tool failed unexpectedly" with `files` left empty.

**4. Two existing bounds would eat the offload, and one names the wrong subject.**
`bound_tool_results` already routes every returned `Command` through `rewritten_command_files` →
`_bounded_file`, charged against `agent_subagent_files_max_chars` (200,000) **minus what the thread's
`files` channel already holds**. Measured on a 200,000-character body: uncut at zero held, 198,998 at
1,000 held, 149,999 at 50,000 held, and **45 characters** once the channel is full — the offload
annihilated. Its notice also reads *"cut N character(s) from a file a helper wrote into its caller's
state"*, which is the wrong subject for a system write. Separately, the ceiling is applied in
**three** places, not one: `frame_connector_results` and `tool_authz.answered_failure` both call
`bounded_for_batch` independently, and the framer is *outer* to `bound_tool_results` — measured, a
result inflated to 100,199 characters still reached the model at 59,999 carrying the inner notice.

**5. The cost, measured rather than estimated.** One 200,000-character offload costs ~201 kB of
`checkpoint_writes` once — written as a delta, not re-serialized per superstep; later turns add
~750–1,500 B each. But `files` is a `DeltaChannel` with `snapshot_frequency=50`, and driven over 55
updates the channel's blob stays at **0 B through update 49** and then lands a full snapshot of the
whole channel — 207,108 B in one row — at update 50, and again on
`DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT`. So the real cost is the offload once *plus* a full
re-serialization of everything in `files` every fiftieth write to it. Any budget has to be a bound on
the channel, not on one offload.

## Decision

**The build is scoped here and taken in the next wave rather than at the tail of this one**, and
what it builds is not what the item said.

- **Keep the cut.** Both ends surviving is worth more than a bare pointer, and the repository already
  decided that once.
- **Add the address to it, not a second mechanism.** The offload is written under `/scratch/`, whose
  write permission already exists, and read with `read_file` — so **no new tool**, which is what
  keeps it out of the request prefix every model call pays for.
- **Bypass `_bounded_file` deliberately**, by attaching the offload *after* `rewritten_command_files`,
  with its own bound on the `files` channel derived from finding 5 rather than borrowed from the
  helper-file budget it is not.
- **Carry the path through the clear** by the validated-regex mechanism `cited_note_ids` already
  uses, which is what finally makes the item's own title true.
- **Upstream's eviction stays suppressed**, and `tests/test_upstream_surface.py` gains the assertion
  that says why — today the suppression is a side effect of one number being larger than another,
  which is exactly the kind of assumption that file exists to stop being invisible.

## Consequences

- **Wave 1 ships five items rather than six.** Stopping here is the point: the measurement arrived
  after the item was scheduled and changed three of its four premises, and shipping a delicate change
  to the most heavily-tested module in the tree on the strength of the superseded framing is how a
  wave ends with a revert in the next one.
- **The next wave's version is cheap and safe**, because every assumption it rests on is now a
  measured number in this record rather than a reading of upstream's source.
- **Nothing is lost by waiting that is not already lost today.** The cut has been destructive for as
  long as it has existed; one wave does not change that, and a wrong offload that the helper-file
  budget silently annihilates would be worse than none.

## What keeps it true

Nothing yet, deliberately — this record scopes a build rather than making one, and a test pinning a
decision to build something later would assert nothing about the code. What it names as owed, the
build owes:

- `tests/test_upstream_surface.py` — that upstream's eviction threshold is above
  `agent_max_tool_result_chars`, so the suppression this repository relies on is asserted rather
  than incidental.
- `tests/test_tool_result_size.py` — that an offloaded result is reachable at the path its notice
  names, and that the `files` channel's bound holds across a session rather than per offload.
- `tests/test_compaction.py` — that a cleared result carries its offload path, and that a connector
  cannot forge one, in the shape `test_a_connectors_result_cannot_forge_a_citation_into_the_placeholder`
  already establishes.
