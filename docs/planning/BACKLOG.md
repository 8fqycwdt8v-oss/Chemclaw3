# BACKLOG

The forty things worth doing next, highest-consequence first. Top = next.

**This is a queue of what is still open, not a log of what was found.** A closed item is **deleted**
from it in the commit that closes it; the commit is the record and `git log` is the history. Do not
strike a row through, do not append "**Done**" under it, and do not add a dated section explaining
that a row above has gone stale. That is exactly how this file reached 4,717 lines and 237 open rows
in twenty-one days, growing about three lines for every line removed — the same failure `DEFERRED.md`
had and D-154 fixed there with this one rule.

**Rows are grouped by what they ask for, not by which review produced them.** A finding's date and
its reviewing pass are provenance, and provenance belongs in
[`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md) — the long-form record of every
row this queue has ever carried, including the ~185 that are open but not in the top forty. When a
queued row needs its full measurement history, that file has it under the review that found it.

**A row must name an anchor in the tree** — a module, a line, a manifest key — so any row can be
checked with one `grep` instead of an argument. A row that cannot name one is not ready to be
queued.

**Promoting and demoting.** Anything in the archive may be promoted into this file when it becomes
the next thing worth doing; anything here that turns out not to be may go back. The queue's length
is the point: forty is what a person can hold, and a forty-first row means deciding which row it
beats.

Related registers: [`DEFERRED.md`](DEFERRED.md) (postponed with the trigger that would revisit each),
[`docs/decisions/`](../decisions/) (why the system is the way it is; its README indexes the record by
topic).

---

## 1 — Untrusted input reaching a privileged surface

- [ ] **Four of the six endpoint-serving connectors are unauthenticated** — [M]. `bo`, `calc`,
      `molfp` and `rxnfp` ship `auth: mode: none`. The NetworkPolicy is the only thing between a pod
      in the namespace and a tool that starts durable work. This row used to read "six of seven, and
      `connectors/manifest.py` carries a `bearer` mode nobody sets" — the capability migration
      closed two of them as a side effect rather than on purpose: `chem` and `safety` are served by
      `Chemclaw3-mcp`, whose `connector_app` enforces a bearer on `/mcp` itself, so their manifests
      had to set `mode: bearer` or every call would be refused. That makes the mode no longer
      theoretical: `CHEMCLAW_CHEM_TOKEN` and `CHEMCLAW_SAFETY_TOKEN` are read per request today, and
      the four remaining are the ones we host.
      *Design direction:* MCP's OAuth 2.1 / ID-JAG token exchange, and `entra_workload` for the
      federated case.

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** —
      [M], half closed. `connectors/server.py`'s `CallerLogMiddleware` reads a header no gate
      verifies, and the value reaches `job_records` and the audit trail as the actor. The record now
      says which half is which (`unverified:<id>`, D-2026-08-13); what is open is that a caller can
      still choose the string. Closes with the row above.

- [ ] **`vector.server_embed_function` reaches the SQL text unchecked** — [L].
      `ingest/eln/warehouse/binding.py` interpolates the configured function name into the query,
      so the module's "only checked identifiers are interpolated" claim is false for this one field.
      A manifest is site-authored configuration, not a credential — but it is also the one field
      here that a non-reviewer edits.

- [ ] **A warehouse row key is interpolated into a filesystem path with no slug validation** — [L].
      `ingest/eln/warehouse/retriever.py:184`. The key comes from the warehouse, so this is a
      traversal that a compromised or merely sloppy upstream table can drive.

- [ ] **ELN free text becomes real knowledge-graph edges** — [M]. `ingest/eln/note.py:27`: a chemist
      writing `contradicts` / `supersedes` into a free-text field forges a relation into a PR-gated
      reaction note. The gate sees a well-formed note, because it is one.

- [ ] **No connector or MCP tool result is ever framed** — [M]. `connectors/*/server/tools.py`;
      `fetch_artifact` hands arbitrary externally-produced text straight to the model. This is the
      widest unframed surface in the tree. Two narrower ones go with it:
      `agent/memory_tools.py:80` (`recall_observations` returns corpus-mined free text) and
      `agent/research_tools.py:181` (`gather_evidence` frames `chunk.content` but not the same
      note's caller-influenced `source`).

- [ ] **The built-in write gate never consults the connector-declared `state_changing` set** — [L].
      `agent/authz.py`'s `DEFAULT_WRITE_TOOL_GATES` is a hand-maintained frozenset while every
      manifest already declares which of its tools change state. Two answers to one question, and
      the hand-maintained one is what runs.
- [ ] **`WarehouseQueryError` embeds the driver's text in a message the model reads** — [S].
      `ingest/eln/warehouse/snowflake.py:88`. Driver errors quote the statement, so the query shape
      and column names reach the transcript.

## 2 — Answers that are wrong without saying so

- [ ] **Split-conformal uncertainty is implemented and unwired** — [S].
      `science/calc/uncertainty.conformal_uncertainty` is correct and tested and has no caller:
      `solubility.py` reports the constant `solubility_rmse_log` instead, so no prediction has ever
      carried an interval derived from this deployment's own residuals. Wiring it is a capability
      decision — which predictors, over which reconciled measurements — not a cleanup.
      *The configuration half is closed*: `calibration_conformal_coverage` and
      `calibration_conformal_min_samples` were deleted (2026-08-14) rather than left as knobs an
      operator could set with no effect; they come back with the caller. The row's weaker third was
      **wrong** and is dropped: `service_uvicorn_workers` has three readers
      (`core/config/__init__.py:195` refuses `>1`, and the fleet connection-budget arithmetic at
      `:208`/`:214` reads it).

- [ ] **Four hazard-screen rules miss a reagent a chemist would expect them to catch** — [M], one
      row because they are one defect class: a SMARTS arm written for the common spelling.
      `peroxide-with-ketone`'s `left` is `[OX2H][OX2H]`, so `Na2O2 + acetone` raises only
      `peroxide`; `complex-hydride-with-chlorinated-solvent`'s is `[$([AlH4-]),$([BH4-])]`, so NaH
      raises nothing; `azide-with-dichloromethane`'s `right` is `[CH2](Cl)Cl`, so chloroform is
      missed; and `core/reagents._TABLE` holds no hydrazine at all, so a hydrazine widening cannot
      be checked. All four measured through the shipped screen.

- [ ] **A solvate collapses onto whichever fragment is larger** — [M]. `standard_smiles("CCN.C1CCOC1")`
      returns THF: `FragmentParent` keeps the largest fragment. Every downstream key, screen and
      similarity hit then describes the solvent.

- [ ] **Mass balance is element-set subsumption only** — [M]. `ingest/eln/validate.py` checks that no
      product element is absent from the inputs, so `benzene + methanol >> paracetamol` validates.

- [ ] **A retracted ELN entry stays current evidence** — [M]. A withdrawn entry that simply
      disappears from the export is invisible to a cursor-based sync, so the note it produced keeps
      answering as current.

- [ ] **One non-UTF-8 ORD export aborts the entire ELN sync batch** — [M].
      `ingest/eln/ord_adapter.py:110`, contradicting the adapter's own skip-and-continue contract.

- [ ] **A BO observation naming an undeclared parameter is silently dropped** — [S], and it is a
      *fabrication* vector rather than an error-handling one: the campaign then optimises against a
      history that is missing the observation the chemist thought they recorded.

- [ ] **A durable campaign's declared direction is not checked against its registered objective** —
      [M]. `CampaignSpec` carries `problem.objectives[0].direction` and the registered objective
      separately; nothing asserts they agree, so a maximise campaign can minimise.

- [ ] **`evals.live`'s per-turn Temporal probe makes `failed_loudly` unconditionally true** — [S].
      `evals/live.py:317`. The harness's headline "failed silently" signal can never fire, which
      makes every live eval run's most important number meaningless.

## 3 — Work that is lost, dropped or invisible

- [ ] **A failed durable job is dropped from the mid-turn resume** — [L]. `agent/job_results.py:83`,
      and the function's own docstring says it is not. The chemist is told nothing.

- [ ] **The mid-turn resume drops `user_input_requests`** — [L]. `api/runner.py:780`: an approval
      prompt raised during a resume never reaches the stream, so the turn waits on an answer nobody
      was asked for.

- [ ] **A template workflow's failure is invisible to the chemist** — [S]. `TemplateWorkflow.run`
      has no `try/except` and reaches `notify_session_best_effort(…, "job_completed", …)` only on
      the success path.

- [ ] **A pinned template's arguments go unchecked once its bundle stops being ours** — [S].
      `cli/validate_templates.py::_resolvable_signatures` reads a signature from
      `connectors/<name>/server/tools.py`, so a bundle we declare but do not run has none —
      `hazard-briefing` calls `screen_hazards` and is name-checked only. The loss is now *reported*
      rather than silent (`unchecked_arguments`, printed on the passing path), which is what makes
      this a queued row instead of a defect. The fix is to check argument names against the running
      server in `make connector-validate`, where a live session already exists and MCP tool specs
      carry input schemas; `make template-validate` must stay offline, so it keeps the note.

- [ ] **A decided approval hold can be reopened, and the obvious fix is worse** — [M].
      `agent/interaction_tools.py::start_approval` passes no `id_reuse_policy`, so temporalio's
      default lets a decided hold be started again under the same id. The archive records why
      `REJECT_DUPLICATE` is not the fix.

- [ ] **A plan no longer shows which step is waiting on a durable job** — [M]. The MAF engine marked
      it by prefixing a todo's description (`awaiting-job:<id>`); the LangGraph rebuild replaced the
      plan store and did not carry the marker, so a waiting plan renders as a stalled one
      (`agent/state.py:6,24`).

- [ ] **A template agent step's token spend is unmetered** — [M]. `record_turn_cost`,
      `chemclaw_tokens_total`, `budget.record`, `begin_call_watch` and `begin_loop_watch` all have
      no call site on the template path, so a template is a budget bypass. The repeat guard and the
      loop cap are inert there for the same reason, and `session_id` is empty on every
      template-path audit row (`run_agent_step` never calls `set_current_session_id`).

- [ ] **No heartbeat and no aggregate timeout on template steps** — [M].
      `template_job.py:139-158` sets `start_to_close_timeout` only; `template_activities.py` never
      calls `activity.heartbeat`, so a wedged step is invisible until the timeout.

- [ ] **A timed-out attachment parse still runs to completion** — [M].
      `parse_attachment_off_loop` bounds how long a caller waits and how many parses run at once; it
      cannot bound the thread, so a hostile document holds a worker forever. `ingest/documents/sync.py:200`
      has the same shape with no timeout at all.

## 4 — Operating it

- [ ] **`LANGSMITH_TRACING` is pinned false in the Helm chart and nowhere else** — [S]. `langsmith`
      is in the runtime closure and enables itself from ambient environment: measured,
      `LANGSMITH_TRACING=true` sends conversation content to `api.smith.langchain.com` with no repo
      code involved. `deploy/entrypoint.sh:18` now exports a default, which covers the image; `make
      chat`, `make connectors`, hand-started workers, CI and local dev are still unguarded. Fix at
      the composition root so it holds regardless of launcher.

- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart
      and no statement of who runs either. Everything below about backup and retention is downstream
      of this.

- [ ] **No backup tooling, and three stores whose recovery is someone else's** — [M]. Nothing in
      this repo performs or verifies a restore.

- [ ] **The background worker is a hard singleton** — [M]. `workers.background.replicas: 1` owns ELN
      sync, memory synthesis, retention and eval drift, and cannot be scaled because the PR-gate
      checkout lock is host-local (D-069). It needs the distributed lock, which is its own
      `DEFERRED.md` row.

- [ ] **Two migrations share the number `037`, and two share `043`** — [XS]. `infra/sql/` holds
      `037_bo_suggestion_provenance.sql` and `037_document_index.sql`, plus
      `043_session_listing.sql` and `043_session_message_shape.sql`. **This row supersedes two
      earlier filings that reached opposite conclusions** (one asked for a rename, one argued
      against). The decision: the runner applies by filename and records by filename, so nothing is
      broken today and a rename would be a destructive edit to merged migrations, which
      `tests/test_migrations_are_additive.py` refuses. So **do not renumber** — add the collision
      check to the migration test instead, so the *next* one is caught before merge.

- [ ] **`.github/workflows/ci.yml` checks out with the `actions/checkout@v4` depth-1 default, which
      makes the migration-immutability check unrunnable in CI** — [S], one line. On a depth-1 clone
      `git show <graft>:file` is the working tree's own content, so a smuggled `ALTER TABLE`
      appended to a merged migration reported no edit across all 42 migrations. The test now skips
      honestly rather than passing; the check itself needs `fetch-depth: 0`.

- [ ] **The image vulnerability scan is written but not merged as a gate** — [M].
      `trivy image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL` exists and does not run.

- [ ] **Egress is still port-scoped by default** — [S]. `networkPolicy.egressDestinations` is
      declarable and empty, which renders `to: []` — any destination on the allowed ports.

- [ ] **Secrets are plain `str` on every `Settings` field, and are never rotated** — [M], corrected:
      `SecretStr` now exists in exactly one place (`agent/llm_provider.py:169`, wrapping the key for
      `ChatOpenAI`). `llm_api_key`, `hpc_api_token`, `temporal_api_key` and the DSN are still plain
      strings on the settings object, one `logger.debug("%s", settings)` from a log line.

- [ ] **No session delete, export, or pagination** — [M]. Only `POST`/`GET /sessions`. A data-subject
      erasure request currently has no route across the seven tables that hold a conversation.

## 5 — Upstream capability this stack already has

- [ ] **`PlanEvent` carries no `plan_hash`** — [S], 2026-08-15. A client watching the stream sees the
      todo list but cannot post `POST /sessions/{id}/plan/decision` without a separate `GET /plan`
      round-trip for the hash it must echo — which is what `evals/live.py` does. Adding the field is
      additive to the SSE union, so it needs a `Chemclaw3_ui` change only to *use* it.

- [ ] **A plan refusal is distinguishable only by substring** — [M], 2026-08-15. It reaches the wire
      as a `ToolFailedEvent`, and every consumer tells it apart by matching the refusal sentence
      (`evals/live.py`'s `PLAN_GATE_MARKER`). The sentence is pinned by a test, which is what keeps
      this working and also what makes it a coupling: a reworded refusal is a silently miscounted
      eval. Wants its own event type or a discriminator field.

- [ ] **`graph_stream._from_update` silently drops `__interrupt__`** — [S], 2026-08-15. It skips any
      update whose payload is not a `dict`, and an interrupt arrives as a tuple — so a turn that
      suspended would end with no answer text and be classified `empty_answer`. Nothing emits one
      today (see the ADR below), so this is latent rather than live, and fixing it without a producer
      is a branch no test can reach. The row exists so whoever adds the first interrupt finds it.

- [ ] **`RubricMiddleware` in the turn loop** — [M]. `agent/verifier.py`'s own docstring says a
      low-confidence answer is "marked, not blocked", and nothing routes a `review_required` answer
      back for another pass. Upstream has the loop.

- [ ] **Checkpoint deletion via `BaseCheckpointSaver.adelete_thread`** — [S].
      `durable/retention.py` and `agent/leaver.py` both hand-roll `DELETE FROM {table} WHERE
      thread_id = …` over `CHECKPOINT_TABLES` (`agent/checkpointer.py:124`) — a hand-maintained
      tuple that is the sole route for both erasure and retention, and that upstream can extend
      without telling anyone.

- [ ] **The front door on `stream_events(version="v3")` — built, measured, reverted; do not restart
      it until upstream reports usage incrementally** — [L]. v3 retires `astream`'s tuple arity, the
      single largest unpromised-shape coupling in the tree, and **all sixteen event-contract
      assertions passed unmodified** (no `Chemclaw3_ui` change needed). The blocker: v3 reports token
      usage only at `message-finish`, so a turn abandoned mid-message books **0** tokens where the
      current driver books ~30 — making "drop the connection just before the answer" a free bypass
      of the token budget. *Restart when v3 emits usage per content block, or exposes the raw
      `(chunk, metadata)` stream alongside the content-block one.* Full measurement in the archive.

- [x] **Nothing checks that a symbol named in prose still exists — closed 2026-08-15.**
      `tests/test_docstring_paths.py` now checks two forms: a path with an optional `::symbol`
      suffix, and a fully-qualified `chemclaw.*` name. It found **19 stale pointers on the first
      run, with no false positives** — including `chemclaw.durable.py`, the blind-substitution
      artifact the gate's own docstring had described since D-149 but could not catch, and
      `chemclaw.agent.session_context`/`identity_context`, which moved to `core/` and were still
      cited in `agent/turn_flags.py`. The `::symbol` half closed a hole that had let two pointers to
      a deleted module survive the commit whose subject was deleting it.
      **Scope was measured, not chosen**: checking every backticked bare identifier flags 1,077 of
      3,351 (`None`, `ValueError`, `await`, SQL table names, upstream types), and `module.symbol`
      flags 426 of 528 because the same shape spells attribute access, a filename and a subpackage.
      The qualified form flags 30 of 764, which is small enough to read one by one. Non-vacuity
      proven by mutation on four classes of dangling reference.

- [ ] **`logging.handleError` prints a malformed record's raw `msg`/`args` to stderr, unredacted** —
      [S]. The deliberate design is that `SecretRedactingFilter.filter` never raises and lets
      logging report the record itself (`core/logging.py:843`) — which routes an unredacted record
      to stderr. Correct against a crash, unresolved against a secret.

---

## Everything else

~185 further open findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md),
grouped by the review that found them, with their full measurements. They are open, not abandoned —
promote one into the queue above when it becomes the next thing worth doing, and delete it from
here when it is done.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Entra tenant, real
Temporal broker, real cluster, real HPC, real Snowflake — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.
