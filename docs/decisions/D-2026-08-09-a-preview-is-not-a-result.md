# D-2026-08-09-a-preview-is-not-a-result — A preview is not a result, so give the result somewhere to live

**Status:** accepted · **Date:** 2026-08-09

## Context

Every tool in this system returns a typed Pydantic model. `screen_hazards` returns a `ScreenResult`
whose flags each carry a `rule_id`, a severity, an explanation and a literature citation.
`stoichiometry_table` returns a `ChargeTable` of rows a chemist could weigh out from.
`calculator_trust` returns a `Calibration`. `compare_solvents` returns a ranking whose own manifest
insists it is a ranking and not a set of absolutes.

None of that reaches a surface. The only thing `ToolResultEvent` carries about what a call returned
is `preview` — 200 characters (`agent_audit_max_arg_chars`), cut at whatever byte the budget lands
on, explicitly not JSON, and documented as staying that way: *"never a whole evidence sweep streamed
to a browser."* So a hazard screen arrives at the chemist as prose the model wrote about it, and the
frontend cannot fix that, because the data has never crossed the wire. A companion study against
this repository at `261b166` reached the same conclusion from the other side and records it as
constraint C1: *parsing the preview is not an option*, and this codebase already says so in three
places.

Two fields have already been split out of `preview` for exactly this reason and by exactly this
argument. `note_ids` was added because a grounding check scored against 200 characters of a
40-chunk sweep called 39 of 40 citations fabricated. `numbers` was added because the re-run with
the ids fixed still called six verbatim ICH limits invented, the figures being still only in the
preview. Both are "prose for a human, machine-readable for a consumer" — and both stop short of the
one thing a *rendering* needs, which is the result's shape.

The full text was never far away. `api/runner_trace.py::_result_text` returns it untruncated — its
docstring says "Untruncated on purpose" — and `feed()` computes the preview, the ids and the numbers
from that one `text` variable before dropping it.

## Decision

**1. The event carries a reference, not a payload.** `ToolResultEvent.result_ref` is the SHA-256 of
the result's own text; a surface that has decided to render one result fetches that one result from
`GET /sessions/{session_id}/tool-results/{ref}`.

The alternative on the table was `data: dict` on the event for a whitelist of results known to be
small (hazard screen, genotox alerts, ICH lookup, charge table, calibration). It is simpler and it
re-opens precisely the question the truncation closed: the budget exists because the stream fans out
to every consumer, and a whitelist is a promise about sizes that a connector author can break
without noticing. A ref costs one round trip *only for what somebody chose to look at*.

**2. The store is content-addressed and two-table, modelled on `019_artifact_store.sql`.** The blob
is keyed by the hash of its content, so a repeated identical call stores nothing — D-011's "never
compute twice" applied to bytes. The link row carries the session, the tool, the correlation id and
a timestamp, and it is what makes a blob reachable.

**3. The route is scoped under the session, and that is the authorization story.** A ref is
unguessable but not secret: anyone who can reproduce a result's text can compute its address. Hung
off `/sessions/{session_id}`, the route resolves through `resolve_session` — the ownership gate the
front door already has — and the store's read joins the link row for the same session on top of it.
A bare `/tool-results/{ref}` would have needed an authorization story invented for it, and the story
it would have ended up with is "the ref is a bearer token".

**4. Over the cap a result is refused, never trimmed, and the refusal is logged.**
`stream_max_result_bytes` (128 KiB, against a largest-measured real result of ~20,000 characters)
bounds what may be written. Trimming would be the worse failure and it is the sharper version of a
rule this repository already keeps: half a `ScreenResult` is still valid JSON, and it renders as a
*complete* hazard screen with flags missing. `_capped_numbers` logs its truncation for the weaker
version of the same reason, and this follows it.

**5. `result_ref` is empty when nothing was stored, with one meaning for three causes** — the store
is off, the result was over the cap, or the write failed. **Storing never fails a turn.** A tool
result reaching a browser is a rendering, and no rendering is worth an answer. The swallow goes
through `degraded(logger, "tool_result_store", …)` rather than a bare log call, because this is the
first `degraded` site on a *per-tool-call* path rather than a per-turn one: a development CLI with
no database emits one line per call, and the counter is the half an operator can alert on.

**6. Retention is a `created_at` cutoff and nothing more.** `durable/retention.py` refuses to prune
`calculation_results` (evicting a cached answer converts a hit into a recomputation, D-011) and
`job_records` (the durable evaluation record, D-157). Those refusals turn on the table holding a
*record*. A tool-result blob holds none — it is a view of a turn that already happened, and losing
one costs a rendering the chemist can ask for again. That is exactly what makes an age cutoff
sufficient, and it is why there is no LRU and no cost ordering: ranking evictions by value only pays
when what is being ranked is expensive to regenerate.

**7. `feed()` becomes a coroutine.** The write has to land before the event naming it is yielded —
announcing a ref and then storing the bytes leaves a window in which a client that follows the ref
finds nothing. The store is reached through an injected `ResultSink` rather than a session id or a
contextvar, so `runner_trace`'s stated property — no ambient state, no session, no contextvars —
stays literally true, and a trace built with no sink behaves exactly as it did.

**8. `ScreenResult` and `AlertResult` echo the canonical SMILES they screened.** Every molecule-
taking calculator canonicalises (`core.chem.require_canonical_smiles`) and the safety screens parse
the same molecule; echoing `Chem.MolToSmiles` of the molecule already in hand gives a client a
stable entity key without shipping RDKit to the browser, and fixes a second thing: a clean screen
used to serialize to `{"flags": [], "verdict": …}`, naming nothing it had looked at. For a result
whose whole discipline is that it must never read as a clearance, that was the wrong thing to be
vague about.

**9. `GET /notes/{note_id}` returns the same `NoteView` `expand_note` returns.** The knowledge graph
was readable by the agent and by nobody else, so a citation chip was a highlight rather than a link.
It reuses the tool's own function, framing envelope included: a second projection of a note would be
a second answer to "what does this note say".

## Consequences

- The frontend can build typed result cards — the study's Concept C — against a fetch route rather
  than against a truncated string. Nothing about the streaming budget changes.
- `tool_result_blobs` is the highest-volume table this system has, at up to one row per tool call,
  and its retention window defaults to 0 like every other window. That is a deliberate uniformity
  rather than a policy for this table: `retention_enabled` is off by default, so a number here would
  differ from 0 only for a deployment that switched retention on without stating this window, which
  is the case `test_retention_is_off_until_a_policy_is_stated` refuses. The cost — unbounded growth
  until an operator states a window — is written in `infra/sql/README.md`'s Disposal column rather
  than implied away.
- `ToolCallTrace.feed` is awaited. The forty synchronous call sites in the suite go through
  `tests.fakes.fed`, which drives one coroutine that never suspends when there is no sink.
- What this deliberately does **not** do: change what any calculator returns. `PkaResult.smiles` is
  already the canonical form it computed on; `SolubilityResult.smiles` is the caller's spelling, and
  making it canonical would change the shape of rows already in `calculation_results`, which is a
  `CALCULATION_EPOCH` bump — cache-wide invalidation for a cosmetic echo. It is left, and said here
  rather than left to be rediscovered.
