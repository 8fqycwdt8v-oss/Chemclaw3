# `src/chemclaw/durable/` — security and hardening

Slice: every module under `src/chemclaw/durable/` (25 files, 5048 LOC), read in full.
Lens: what untrusted input reaches, what fails open, what leaks.

Five findings below, three of them reproduced by running code. A short "checked and clean"
list follows at the end so the negative results are on the record too.

---

## A durable job's failure text is persisted and displayed with neither redaction nor a length bound

- **Severity**: medium
- **Location**: `src/chemclaw/durable/connector_job.py:62` (`failure_reason`), consumed at
  `src/chemclaw/durable/connector_job.py:323-342` (`ConnectorJobWorkflow._notify_failure`),
  `src/chemclaw/durable/template_job.py:149-169` (`TemplateWorkflow._notify_failure`) and
  `src/chemclaw/connectors/jobs.py:504-506`
- **Trigger**: any connector job whose child workflow fails. `failure_reason` walks past the two
  structural Temporal frames and returns `str(cause)` of the first application-level error —
  verbatim, whatever it contains. Two concrete producers exist in-tree:
  - `src/chemclaw/connectors/qm/hpc/nextflow.py:116` — `NextflowError(f"launch failed: {error_detail(response)}")`,
    where `error_detail` (`src/chemclaw/core/http.py:55`) deliberately embeds the upstream HTTP
    response body. The launcher is authenticated with `hpc_api_token`, which is listed in this
    repo's own secret inventory (`src/chemclaw/core/logging.py:449-464`).
  - `src/chemclaw/connectors/qm/activities.py:155` — `ValueError(f"unparseable QM output: {raw_output!r}")`,
    where `raw_output` is the whole artifact fetched from `hpc_artifact_store_url` (`nextflow.py:165`,
    `return response.text`). No cap anywhere on that path.
- **Consequence**: the string is written verbatim into three places, none of which redact:
  1. `session_events.payload->>'reason'` — a persisted `jsonb` column
     (`chemclaw/durable/notify.py:70-91` → `chemclaw/agent/session_events.py:75-90`), which
     retention **cannot** dispose of unless the row is consumed (see the next finding).
  2. the SSE stream to the browser — `chemclaw/api/routes/streams.py:116`,
     `reason = str(pushed.payload.get("reason", ""))` → `JobFailedEvent`.
  3. the model's context, when the job fails inside the turn —
     `chemclaw/connectors/jobs.py:505` raises `ConnectorJobError(f"... {failure_reason(...)}")`,
     and `ValueError`-family errors are explicitly passed through `_sanitize_tool_errors`
     unchanged. That text is then sent to the LLM provider.

  This repository already established both controls for exactly this class of string and applies
  them one module over: `kg/pr_gate.py:140` does
  `reason = redact_secrets(str(exc))[: settings.proposal_reason_chars]`, with a docstring naming
  the precise failure ("a realistic token-bearing push failure measures well under any length
  worth keeping, so the credential was stored verbatim and in full"). `failure_reason`'s own
  ~30-line docstring is entirely about *which frame* to pick and says nothing about what the
  frame may contain.
- **Evidence**: `/tmp/repro_reason.py`, run with `uv run`:

  ```
  failure_reason  : NextflowError: launch failed: 401 Unauthorized: {"message":"Invalid token: sk-hpc-supersecret-token-value-123456"}
  redact_secrets  : NextflowError: launch failed: 401 Unauthorized: {"message":"Invalid token: ***"}
  token in reason?: True
  blob reason length: 400037
  ```

  The second line is the point: `redact_secrets` — already imported by `pr_gate`, already
  configured with `hpc_api_token` — catches exactly what `failure_reason` emits unredacted.
  The last line is a 400 KB `reason` travelling into a `jsonb` column and down an SSE pipe.
- **Fix**: bound and redact at the one place that produces the string. In
  `durable/connector_job.py`:

  ```python
  from chemclaw.core.logging import redact_secrets
  ...
  return redact_secrets(str(cause) or type(cause).__name__)[: settings.job_failure_reason_chars]
  ```

  `redact_secrets` is a pure `core` function with no `connectors` import, so it is safe inside the
  workflow sandbox. Add `job_failure_reason_chars` beside `proposal_reason_chars` in config rather
  than a literal. Both `_notify_failure` sites and `connectors/jobs.py:505` inherit the fix,
  because all three already call this one function.

---

## The approval hold's PR-gate write is recorded with an empty actor, so its owner cannot see it

- **Severity**: medium
- **Location**: `src/chemclaw/durable/interaction_approval.py:59-69`
  (`propose_confirmed_answer_activity`)
- **Trigger**: a chemist clicks "Yes" on a confirmed-answer hold. `InteractionApprovalWorkflow.run`
  executes this activity with the `InteractionCandidate`, which carries `requested_by` — and the
  activity ignores it. `propose_confirmed_answer` → `propose_note` → `ambient_provenance()`
  (`kg/proposal.py:313`), which reads `get_current_actor()`. An activity sets no ambient identity,
  so the recorded `NoteProposal.actor` is `""`.
- **Consequence**: the durable compliance record of an *authorized human knowledge write* names
  nobody. Downstream, `list_proposals(state, actor, ...)` scopes a non-reviewer's queue to
  `principal.oid` and `chemclaw/api/deps.py:196` 404s the detail view unless
  `proposal.actor == principal.oid` — so the chemist who approved the note cannot find, read or
  act on the PR opened on their behalf. It fails closed rather than open, which is why this is
  medium and not high; the harm is a blank attribution in the record and an unreachable surface.

  This is the *third* copy of a defect the same package documents having fixed twice.
  `memory_jobs.publish_memory_note_activity` (`memory_jobs.py:135-163`) takes an `actor` and
  stamps it, giving this exact reason. `report_workflow.propose_report` (`report_workflow.py:85-103`)
  does the same and says "This was missed in the first pass: the memory-note path was fixed and
  this one was not". The approval path was missed in both passes, while
  `InteractionCandidate.requested_by`'s docstring (`interaction_approval.py:34-40`) asserts the
  field exists so the write can be attributed correctly.
- **Evidence**: `/tmp/repro_actor.py`, run with `uv run` against the in-memory proposal store
  (the backend a `session_store="memory"` deployment actually gets):

  ```
  hold ref: branch:note/interaction-q1
  memory ref: branch:note/playbook-x
  proposal note_id='playbook-x'      actor='oid-alice' session=''
  proposal note_id='interaction-q1'  actor=''          session=''
  scoped to oid-alice: ['playbook-x']
  ```

  Same actor, same process, two paths — the memory path records `oid-alice`, the approval path
  records `''`, and the owner-scoped listing drops the interaction note.
- **Fix**: stamp the candidate's actor for the duration of the gate, the same three lines the two
  sibling activities use:

  ```python
  @durable_activity("background")
  @activity.defn
  async def propose_confirmed_answer_activity(candidate: InteractionCandidate) -> str:
      token = set_current_identity(candidate.requested_by, frozenset()) if candidate.requested_by else None
      try:
          return await propose_confirmed_answer(...)
      finally:
          if token is not None:
              reset_current_identity(token)
  ```

  Better still: extract the stamp-then-propose block once (it is now a third caller — Rule of
  Three) so a fourth path cannot forget it.

---

## A model-authored `rationale` injects arbitrary typed graph relations into every PR-gated connector note

- **Severity**: medium
- **Location**: `src/chemclaw/durable/job_record.py:199-220` (`note_with_run_provenance`),
  called from `src/chemclaw/durable/connector_job.py:374`
- **Trigger**: any connector job with `publish_to_graph` whose `rationale` contains `[[...]]`.
  `rationale` is a free-text argument the LLM fills in (`connectors/jobs.py:332,342` validates only
  that it is non-blank) and it is interpolated raw into the note body footer.
  `Note.outgoing_links` / `outgoing_relations` parse `[[...]]` out of the body
  (`kg/note.py:442-455`), so anything the model writes there becomes a real graph edge.
- **Consequence**: two things, and the first contradicts the code's own comment. The docstring at
  `job_record.py:208-211` states: *"The footer carries **no `[[wikilink]]`**, deliberately. A link
  to a note that does not exist fails `chemclaw.kg.validate` on the very PR this note opens"*. The
  code one line below interpolates an unvalidated free-text field into that same footer, so the
  property the comment asserts does not hold for any value the model chooses.
  1. **Relation injection.** `[[supersedes: playbook-safety-rule]]` inside a plausible sentence
     mints a `supersedes` edge in a document a reviewer reads as run metadata, not as content.
     The agent is the author of that field and the agent is the component exposed to
     prompt-injected retrieved material, so this is a path from injected text to a knowledge-graph
     edge. The PR-gate keeps a human in the loop, which caps this at medium — but the footer is
     the part of the diff a reviewer is least likely to read as assertions.
  2. **Publish denial.** A dangling target fails `kg-validate` on the PR the job just opened, and
     `publish_note_best_effort` (`durable/publish.py:203`) swallows every failure on this path, so
     the job reports success while its knowledge contribution is stuck on a red branch.
- **Evidence**: `/tmp/repro_rationale.py`, run with `uv run`:

  ```
  BODY:
  The screen finished.

  Why this ran: follow up on [[supersedes: playbook-safety-rule]] and cite [[compound-does-not-exist]]

  - run: `connector-calc-x` (calc/compare_solvents)
  - requested by: oid-alice

  outgoing_links: ['playbook-safety-rule', 'compound-does-not-exist']
  outgoing_relations: [Relation(rel='supersedes', to='playbook-safety-rule'),
                       Relation(rel='cites',      to='compound-does-not-exist')]
  ```

  The same shape exists at `src/chemclaw/durable/observation_jobs.py:114-122`
  (`_promotion_summary`), which interpolates `observation.statement` and
  `', '.join(observation.projects_seen)` into a note body; `projects_seen` is
  `OrdReaction.project`, a value that arrives from the ELN rather than from this system.
- **Fix**: neutralize the markup at the interpolation site — the field is prose, not markup, and
  nothing about the run's reason needs to be linkable. One helper in `kg/note.py` beside `WIKILINK`:

  ```python
  def escape_wikilinks(text: str) -> str:
      """Render `[[...]]` inert: this text is quoted prose, not authored graph markup."""
      return text.replace("[[", "[​[")
  ```

  applied to `record.rationale` in `note_with_run_provenance` and to `observation.statement` /
  `projects_seen` in `_promotion_summary`. Then make the docstring's claim testable
  (`assert not stamped_note.outgoing_links()` for a hostile rationale) so it stops being prose.

---

## `session_events` has no disposal path for the kinds nothing consumes, in the table retention claims to bound

- **Severity**: low
- **Location**: `src/chemclaw/durable/retention.py:122` (`_PRUNABLE["session_events"]`), with the
  producers at `src/chemclaw/durable/digest.py:146-150` and
  `src/chemclaw/durable/eval_drift.py:105`
- **Trigger**: enable retention (`retention_enabled`) plus either `digest_enabled` or
  `eval_drift_enabled`, and let the deployment run.
- **Consequence**: the prune predicate is `consumed_at IS NOT NULL`, and the **only** consumer of
  `session_events` in the tree is the SSE route, which claims exactly two kinds:
  `chemclaw/api/routes/streams.py:112`, `kinds=("job_completed", "job_failed")`. Four kinds are
  produced. So:
  - `eval_drift` rows (`DRIFT_ALERT_CHANNEL = "system-eval-drift"`) are never consumed and
    therefore never deletable — the module's own comment at `retention.py:108-110` acknowledges
    this and keeps the predicate anyway;
  - `digest` rows land on `_digest_channel(owner) = f"digest-{owner}"`
    (`digest.py:170-172`), a session id no route can ever serve (session ids are server-minted
    `uuid4().hex`, `api/routes/sessions.py:50`), so every digest firing writes a permanently
    undeletable row carrying the subscriber's oid, their query text and the matched note ids;
  - `job_completed` / `job_failed` for a session nobody ever reconnects to are equally permanent —
    and per the finding above, those carry the unredacted failure text.

  `RetentionOutcome.deleted["session_events"]` counts only the consumed rows, so an operator
  reading the job's own audit record sees a healthy number while the undeletable class grows
  without bound. That is precisely the reading `sessions_deferred` / `threads_deferred` were added
  to prevent for the other two tables.
- **Evidence**: code only — `retention.py:121-126` (the predicate),
  `grep -rn "claim_unconsumed\|kinds=" src/chemclaw` returns exactly one consumer
  (`streams.py:112`) with a two-kind filter, against four `notify_session*` kinds
  (`connector_job.py:334,380`, `template_job.py:135,163`, `eval_drift.py:105`, `digest.py:148`).
- **Fix**: give an unconsumed row a second, longer window rather than immortality — e.g. an
  `unconsumed_retention_session_events_days` (default 0/off, like its siblings) so a deployment can
  state a policy for undelivered notifications, and exclude the kinds it deliberately keeps by
  name rather than by the accident of nobody having read them. Separately, decide whether the
  digest channel should exist at all: a delivery channel with no reader is a write-only PII sink.

---

## The `session_messages` prune re-selects the same first N sessions every pass, so a skipped session can stop the sweep permanently

- **Severity**: low
- **Location**: `src/chemclaw/durable/retention.py:152-156` (`_EXPIRED_SESSIONS`) and
  `retention.py:295-323` (`_prune_session_messages`)
- **Trigger**: `retention_max_sessions_per_pass` or more sessions, low in `session_id` sort order,
  that this sweep refuses. A session is refused when `unreadable_rows(rows)` is non-empty
  (`retention.py:305-315`) — one row whose `message_shape` matches neither stored serialization
  (`agent/message_pairing.py:99-108` returns `None`) refuses the whole session, and
  `droppable_rows` then returns the empty set for it.
- **Consequence**: the batch is `SELECT DISTINCT session_id ... ORDER BY session_id LIMIT cap + 1`
  — deterministic and identical on every pass — and a refused session is `continue`d without being
  excluded from the next selection. Nothing in the tree ever repairs an unreadable row (the
  read-time repair went with the MAF thread, per the module docstring), so the refusal is
  permanent, not "self-correcting" as `droppable_rows`' docstring claims for it (*"the next pass
  sees the same rows once somebody has looked at them"* — nobody looks). With `cap` such sessions
  present, `session_messages` retention makes zero progress for every other session, forever, while
  the job reports `deleted["session_messages"] = 0` and `sessions_deferred > 0`, which reads as
  "the next pass has work" rather than "the next pass will do exactly this again".
- **Evidence**: code only (I did not stand up Postgres for this one; the ordering is a literal in
  the SQL and the `continue` is unconditional, so the loop is provable by reading). The two claims
  it contradicts are `retention.py:150-151` (*"A bounded batch makes progress on every pass and the
  schedule drains the tail"*) and `message_pairing.py:246-247` (*"Refusing the session is
  self-correcting"*).
- **Fix**: make the batch skip what it has refused rather than re-select it. Cheapest correct
  version: select `cap + 1` sessions **excluding** those whose rows are unreadable, by carrying a
  per-pass exclusion cursor (`AND session_id > %s`) and paging forward, so a refused session costs
  one slot once and the pass moves on. Emit a distinct `sessions_refused` count in
  `RetentionOutcome` so "stuck" is visible in the job's own record instead of being indistinguishable
  from "deferred".

---

## Checked through this lens and found clean

Recording the negatives so the coverage is legible:

- **SQL.** The two f-string interpolations (`retention.py:252-256` and `retention.py:384-387`)
  interpolate only from the closed module constants `_PRUNABLE` and `CHECKPOINT_TABLES`; every
  value is bound. `artifact_eviction.py`'s two statements bind their parameters.
  `job_record_store.py` binds everything. No string-built SQL reaches a caller's input.
- **Path traversal / git-ref injection.** Note ids become `<type>/<id>.md` and `note/<id>` in the
  PR-gate; `kg/note.py:161` constrains them to `^[A-Za-z0-9][A-Za-z0-9_.-]*$` — no `/`, no leading
  dot. `observation_jobs.py:99-101` mints its id through that same model.
- **Identity carriers.** `core/identity_context.py` is `ContextVar`-based, so the identity stamps
  in `report_workflow.retrieve_section`, `memory_jobs.publish_memory_note_activity` and the three
  `template_activities` entry points cannot bleed between concurrent activities on the shared
  background worker (`worker_max_concurrent_activities`), and every one of them resets in a
  `finally`.
- **Privilege sourcing.** `TemplateRunInput.roles` and `ReportRequest.requested_roles` — the two
  fields that are stamped as ambient roles inside an activity and therefore decide entitlement —
  come from `sorted(get_current_roles())` at the launch site (`templates/registry.py`,
  `agent/durable_tools.py:190`), never from a tool argument. Likewise `session_id`
  (`get_current_session_id()`) and `requested_by` (`require_actor()`). A model cannot author any of
  the four. `ConnectorJobInput.workflow` / `task_queue` / `publish_to_graph` come from the manifest
  and `bundle_queue(connector)`, not from `payload`.
- **The template job step's authorization.** `template_activities.authorize_job_step` really does
  stamp the step's identity before `prepare_job_launch` and returns the validated payload, and
  `template_job._run_job_step` really does start the child with `resolved.payload` rather than the
  raw arguments — the D-168 claim holds as written.
- **Approval-hold ownership.** `InteractionApprovalWorkflow.owner` returns `""` when `_candidate`
  is unset, and `api/deps.py:_owner_authorizes` treats a falsy owner as nobody's under
  `entra_required` — it fails closed. `/approvals` is scoped to `principal.oid`, and both
  `{approval_id}` routes carry `Depends(owned_approval)`.
- **Unauthenticated sub-application.** `durable/serve.py` mounts `core/worker_http.py` on
  `0.0.0.0:9000` with no auth. Its docstring claims "the exposition carries counts and capacity
  only — never a session, a user, or turn content"; I checked every metric label in the tree
  (`scope`, `source`, `state`, `subsystem`, `tool`, `connector`) and the claim holds.
- **SSRF.** No outbound HTTP client is constructed anywhere in this slice from a caller-supplied
  address; the only network peers are the configured Temporal broker, Postgres, the git remote and
  mounted shares.
- **Dynamic import / deserialization.** `durable/registry.py` keys on names produced by decorators
  at import time and rejects cross-module name collisions; nothing in the slice imports by a
  config- or payload-derived name, and no `pickle`/`yaml.load`/`eval` appears.
- **`job_record_store` ILIKE metacharacters.** `pattern = f"%{text}%"` does not escape `%`/`_`/`\`,
  so `GET /jobs?text=_` matches every row — but `list_jobs` is deliberately unscoped
  (`api/routes/jobs.py:26-34` states the position), so wildcard control buys a caller nothing it
  did not already have. Noted, not filed.
