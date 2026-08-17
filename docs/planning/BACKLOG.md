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

- [ ] **`tblite` is a runtime dependency with no importer, and the solvent gate is derived from
      it** — [M]. `pyproject.toml:152`; no module in `src/` imports it, and
      `tests/test_third_party_layering.py:144` forbids one. It survives because
      `tests/test_solvents.py` re-derives `ALPB_SOLVENTS` against the installed copy — but that gate
      launches four durable jobs and the parameterisation that decides it is now the *server's*, so
      the two can diverge in both directions. Removing the dependency needs a replacement source for
      the list, which is a cross-repo contract.

- [ ] **The stored-message conversion is a destructive in-place rewrite, run as a pre-upgrade
      hook** — [M]. `agent/message_migration.py:242` overwrites `session_messages.message` while its
      own docstring and `043_session_message_shape.sql:22` both promise the original stays readable.
      `migrate-job.yaml:10` runs it *before* any new pod exists, so it rewrites data the previous
      release is still serving with a reader that raises `TypeError` on the new shape — and
      `helm rollback` stays broken. Needs an ADR: preserved-original column, post-upgrade hook, or a
      read-side shim.

- [ ] **A local env var shapes half of every remote cache key** — [M].
      `science/calc/models.py:105` rounds coordinates to `settings.xtb_geometry_decimals` before the
      structure crosses the wire, so it is the bytes the server derives `input_hash` from. Changing
      it in one deployment makes every relaxation, Hessian, scan point and CREST search miss
      forever, silently. `tests/test_calc_remote.py:213` guards only the version half of the key.

- [ ] **No live lane in this repo can start** — [M]. `infra/live/processes.sh:47` pins
      `CHEMCLAW_CONNECTORS_REQUIRED=true` while chem, safety and calc are dialled and never started;
      `cli/connectors_dev.py:78` emits URLs only for bundles with a local app, so those three keep
      their loopback defaults. `make live-e2e-full-stack` starts only `props` and `rxnpredict`. Also
      `infra/live/e2e-full-stack/up.sh:185` puts `$MCP_REPO/manifests` on `CHEMCLAW_CONNECTORS_DIR`,
      which `connectors/calc/connector.yaml:13` explicitly forbids — it survives only on
      `registry.py:_bundle_dirs` being first-dir-wins, which nothing pins.

- [ ] **A retrieval leg that raised is indistinguishable from one that found nothing** — [M].
      `retrieval/fanout.py:100` swallows to `chunks = []` and reports `chunks: 0`, which
      `EvidenceSourceEvent` promises distinguishes "nothing to say" from "crowded out" — and
      distinguishes neither from "broken". `chemclaw_evidence_source_failures_total` carries no
      `source` label while the chunks counter does, so the two cannot be joined. This is
      `D-2026-08-01-a-cap-that-starves-a-source` again. No per-leg timeout either.

- [ ] **The audit trail's `agent` column can never be non-empty** — [S]. `agent/audit.py:310` reads
      `get_current_specialist()`, and nothing in `src/` calls `set_current_specialist` —
      `core/turn_signals.record_handoff` has no caller at all. So every subagent tool call is
      recorded identically to the supervisor's, and `D-2026-08-10`'s "records the specialist beside
      the human" is false in the data. Either wire it at the one place a helper is invoked (and add
      the column to `cli/explain.py:37`'s select), or delete the producers and the claim.

- [ ] **Retention's checkpoint `LIMIT` bounds the deletes, not the scan** — [S].
      `durable/retention.py:138` aggregates the whole `checkpoints` table on an unindexed expression
      before `LIMIT` can discard anything, under a per-statement `statement_timeout` — so a first
      pass on a large table is cancelled, retried, and never progresses. `scratchpad.py:74` also
      claims retention prunes the memory tables; `_PRUNABLE` contains none of them.

- [ ] **`message_from_row` degrades on one branch and mislabels the speaker on the other** — [S].
      `agent/session_store.py:84` guards only the MAF branch, so a bad `langchain` row fails the
      whole transcript; the fallback at `:96` always returns `AIMessage`, so a chemist's own
      question can render as something the agent said. A malformed `contents` raises
      `AttributeError` past both callers' handlers (`message_migration.py:82`).

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

## 2 — Answers that are wrong without saying so

- [ ] **On `openai_compatible`, one unsupported `response_format` degrades every judged answer for
      the life of the deployment** — [S]. Measured against a real loopback server, not argued: a
      server that rejects `response_format` with a 400, or ignores it and returns prose, lands in
      `agent/verifier.py`'s bare `except Exception` and degrades to the citation gate on *every*
      call. The same contradicted-citation answer a working judge scores `confidence=0.0,
      unsupported=True` comes back `confidence=1.0, unsupported=False`, because the citation gate
      can only see that a citation resolves, not that the evidence contradicts the claim.
      `score_answer` catches it — `verifier.py:448` forces `review_required` whenever
      `verified_by != "judge"` — so nothing unsafe reaches a chemist *provided every caller goes
      through `score_answer`*; a caller reading `VerificationResult` directly sees the inverted,
      over-confident verdict with only `chemclaw_verifier_degraded_total` to say otherwise. The fix
      is a pre-flight capability probe: one throwaway structured call when `verifier_enabled` turns
      on, failing loudly at startup the way `_require_anthropic_key()` already does, instead of
      silently per call. Anthropic is unaffected.

- [ ] **Split-conformal uncertainty is implemented and unwired** — [S].
      `science/calc/uncertainty.conformal_uncertainty` is correct and tested and has no caller:
      the solubility model reports the constant `solubility_rmse_log` instead, so no prediction has
      ever carried an interval derived from this deployment's own residuals. **The predictor moved
      to `Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`) and the residuals did
      not** — the calibration ledger is this repository's — so wiring it now also has to answer
      where the interval is attached: on the server, which cannot see the ledger, or here, over a
      payload the server produced. Wiring it is a capability
      decision — which predictors, over which reconciled measurements — not a cleanup.
      *The configuration half is closed*: `calibration_conformal_coverage` and
      `calibration_conformal_min_samples` were deleted (2026-08-14) rather than left as knobs an
      operator could set with no effect; they come back with the caller. The row's weaker third was
      **wrong** and is dropped: `service_uvicorn_workers` has three readers
      (`core/config/__init__.py:195` refuses `>1`, and the fleet connection-budget arithmetic at
      `:208`/`:214` reads it).

- [ ] **A solvate collapses onto whichever fragment is larger** — [M]. `standard_smiles("CCN.C1CCOC1")`
      returns THF: `FragmentParent` keeps the largest fragment. Every downstream key, screen and
      similarity hit then describes the solvent.

- [ ] **Mass balance is element-set subsumption only** — [M]. `ingest/eln/validate.py` checks that no
      product element is absent from the inputs, so `benzene + methanol >> paracetamol` validates.

- [ ] **A retracted ELN entry stays current evidence** — [M]. A withdrawn entry that simply
      disappears from the export is invisible to a cursor-based sync, so the note it produced keeps
      answering as current.


- [ ] **A BO observation naming an undeclared parameter is silently dropped** — [S], and it is a
      *fabrication* vector rather than an error-handling one: the campaign then optimises against a
      history that is missing the observation the chemist thought they recorded.

- [ ] **`CalculationKey`'s primary key is an unescaped concatenation of caller-shaped strings** —
      [M]. `science/calc/store.py:122` (`CalculationKey.as_str`) builds the literal
      `calculation_results` primary key as `f"{calc_type}@{calc_version}:{input_hash}:{params_hash}"`
      (`infra/sql/001_calculation_results.sql:7`), and `calc_version` is not guaranteed free of `@`
      or `:` — `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` gives real
      examples (`esol-delaney@2004`, `cal-0.28733:-29.3116`). Two different `(calc_type,
      calc_version)` pairs can serialise to the identical string (`calc_type="a", calc_version="b@c"`
      vs. `calc_type="a@b", calc_version="c"`); if the hash pair also matched, one calculator's
      `ON CONFLICT (key) DO UPDATE` (`science/calc/postgres_store.py:32`) would silently overwrite
      another calculator's cached row with a different `result`. The fix — deriving the key from
      `stable_hash` over the four components as a mapping, the way `molecule_hash`/`input_hash`
      already do — changes every existing row's key, which under D-011 ("never recomputed") is a
      full-cache invalidation on deploy; that trade needs an ADR and a migration plan, not a quiet
      change to `as_str`.

- [ ] **`session_owners` and `session_turns` grow without any age-based disposal** — [S].
      `infra/sql/README.md`'s own `session_owners` row already flags this ("survives its session's
      pruned history; BACKLOG") but no row existed here to match it — this closes that dangling
      cross-reference. Neither table is in `durable/retention.py`'s `_PRUNABLE` set, and the only
      `DELETE` against either is `agent/leaver.py`'s manual, actor-scoped erasure — so every session a
      client ever created (the companion UI creates one on the first keystroke, before any message is
      sent) leaves a `session_owners` row forever, even after `session_messages` for that session is
      fully pruned by age. Needs a policy decision — prune once a session has no remaining
      `session_messages` and is past the retention window, or explicitly accept unbounded growth and
      say so — not a code change made unilaterally.

- [ ] **`observations_status_idx` does not cover the query it was built for** — [S].
      `infra/sql/025_observations.sql:50` indexes `(status, last_seen DESC)`, with a comment saying
      "the retrieval bucket wants open observations newest-first" — but `memory/observations.py:122`
      (`_SELECT_OPEN`) actually sorts `ORDER BY cardinality(evidence_note_ids) DESC, last_seen DESC`,
      an expression the index does not cover. The index serves the `status='open'` filter only; every
      read still sorts all open rows in memory by an unindexed expression. Whether the fix is an
      expression index matching the real sort or a correction to which one is authoritative is a
      product call — the migration's stated rationale and the code that ships disagree about what the
      "newest and most-evidenced first" bucket actually orders by.



## 3 — Work that is lost, dropped or invisible

- [ ] **The digest is written to a mailbox with no reader, and the watermark advances anyway** —
      [L]. `durable/digest.py:146-166` writes to `session_events` under session id `digest-<owner>`
      with `kind="digest"`, and the only consumer in the tree is `GET /sessions/{id}/events`, which
      claims `kinds=("job_completed","job_failed")` and sits behind `resolve_session` — so it 404s
      that id and would filter the kind out anyway. Measured: the route's exact claim against a real
      `digest` row returned `[]` and left it unconsumed. `notify_session_best_effort` returns `True`
      on a successful *insert*, so `acknowledge_digest` fires and `mark_reported` moves the
      watermark past notes the subscriber will never see; `_is_new` can never re-qualify them, and
      `retention.py:122` (`consumed_at IS NOT NULL`) makes the orphaned rows immortal. The same
      dead end exists for `system-eval-drift`, whose must-deliver stance therefore guarantees
      delivery to nobody. Needs a route (`GET /digests` claiming `kinds=("digest",)`) — and until
      one exists, `digest_enabled` should plan no Schedule, since shipping the ack without the
      reader loses matches permanently rather than merely not delivering them.

- [ ] **A rejoined durable run never reaches the second chemist** — [M].
      `connectors/jobs.py:386-403`: on `WorkflowAlreadyStartedError` the launcher returns the id and
      deliberately emits no `record_job_started`, and the running workflow's `session_id` belongs to
      the *first* launcher. So chemist B gets no turn-stream `job_started`, no `job_completed`, and
      `agent/job_results.py` cannot wait on it either — they are told "in progress" and must poll by
      hand forever. The comment justifies the silence with "it may already be finished";
      `handle.describe()` answers exactly that question, so the ~3-line fix is to describe once on
      the rejoin path and announce it when the status is RUNNING. Full push-back to a second session
      is the larger change behind it.

- [ ] **The sixteen periodic workflows can still hang instead of failing** — [M]. The job path now
      declares `failure_exception_types` and `tests/test_workflow_registry.py` holds it
      (`D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs`), scoped deliberately: for a run
      nobody is waiting on, parking a redeploy bug until someone ships a fix is a defensible trade
      and the opposite of the one taken there. Decide it per workflow rather than by widening the
      test — retention and the memory jobs are the ones worth arguing about, since a parked run
      there is invisible in exactly the way the fan-out drop was.

- [ ] **`connector_job_timeout_seconds` bounds a 20-second job and a 24-hour job identically** —
      [M]. `core/config/connectors.py:71`: one global 90,000 s ceiling is the child's
      `execution_timeout` for every bundle, so if the `calc` worker is down a 20 s xTB job sits
      `running` for a day with no signal, while the setting is sized entirely by the QM path. An
      optional `JobSpec.timeout_seconds` applied as `min(declared, setting)` would let a bundle
      lower its own ceiling while the deployment keeps the maximum, leaving
      `_the_job_ceiling_covers_the_poll_it_bounds` untouched.

- [ ] **The mid-turn resume drops `user_input_requests`** — [L]. `api/runner.py:780`: an approval
      prompt raised during a resume never reaches the stream, so the turn waits on an answer nobody
      was asked for.

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

- [ ] **`connectors.<name>.enabled` in the chart never reaches the agent** — [M].
      `values.yaml:135` says "CHEMCLAW_CONNECTORS_ENABLED in `config` below decides which bundles
      the agent loads at all" — and that key is in none of the 33 `config` entries. The chart derives
      `CHEMCLAW_CONNECTOR_URLS`, `SERVICE_FLEET_REPLICAS` and `PG_FLEET_POOLED_PROCESSES` from
      `.Values.connectors` and not the enable list, so `enabled: false` removes the pods and leaves
      the tool on the agent's surface: the launcher starts the wrapper on the polled queue and its
      child on `connector-qm`, which nobody polls, and the chemist is told "running" until the 25 h
      ceiling. Latent today (all seven shipped entries are `enabled: true`); it fires the first time
      someone uses the switch the file documents. Fix is a `chemclaw.connectorsEnabled` helper
      mirroring `connectorUrls`, plus deleting the sentence that points at the absent key.

- [ ] **A jobs-only bundle has no reachability signal at all** — [M]. `connectors/health.py:81-99`
      derives its target from `health_url(manifest)`, which is `None` for a bundle with no
      `endpoint:` — so `qm` reports `unprobed` whether its worker fleet is at two replicas or zero,
      `chemclaw_connectors_unhealthy` counts only `unreachable`, and `check_connectors_at_startup`
      raises only on `unreachable`. The fail-fast posture an operator opts into is structurally
      blind to the failure with the largest blast radius. `describe_task_queue(bundle_queue(name))`
      in the same sweep, reported as `unpolled` and counted like `unreachable`, is the runtime twin
      of the manifest check `connector-validate` now does — and it catches the row above too.

- [ ] **One `replicas` knob drives two differently-shaped Deployments** — [S].
      `templates/deployment-connectors.yaml:35` and `:98` both read `$cfg.replicas`, so scaling
      `calc`'s MCP server to 4 also scales its Temporal worker to 4, and `pooledProcesses` counts it
      twice against the `pg_fleet_max_connections` startup ceiling. Worse, the guard requires
      `replicas` only when there is no `url`, while the worker block is deliberately not conditioned
      on `url` — so a `url:` bundle that owns durable work renders an empty `replicas` (Kubernetes
      defaults to 1) and contributes `nil | int` = 0 to the declared fleet. Split into
      `serverReplicas`/`workerReplicas` defaulting to `replicas`, and extend the chart test to
      require it whenever `worker` is set.

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

- [ ] **The answer judge names no claims on 88% of turns** — [M]. `agent/verifier.py`'s
      `VerificationResult.claims` is what tells a reader *which* sentence is unsupported, and
      measured over 51 live turns it was non-empty on **6**. 26 of 39 flagged answers carry an empty
      `unsupported` list, so `review_required` says "something here is wrong" and nothing else — to
      a chemist, to a surface, or to any future revision loop. Fix is in the judge's prompt and its
      structured schema, not in the caller.

- [ ] **The judge's verdict is not reproducible at the threshold** — [M]. Re-scoring 39 unchanged
      answers cleared **5.1% per roll**, so a `review_required` flip can mean the judge rolled
      again rather than that anything changed. It is a margin effect, not general noise: two probes
      on unambiguous fully-grounded answers scored 1.00 six times out of six. But
      `verifier_confidence_threshold` (0.7) is precisely where the margin is. Either the judge needs
      to be made deterministic enough to gate on (temperature, a stricter rubric, or best-of-n), or
      the threshold needs a hysteresis band — and until one of those, no automated consumer should
      act on a single reading.

- [ ] **A flagged answer is never revised** — [M]. `agent/verifier.py`'s own docstring says a
      low-confidence answer is "marked, not blocked", and nothing routes a `review_required` answer
      back for another pass. **`RubricMiddleware` is not the fix**
      (`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer`): it builds a grader
      of its own with no seam to reuse `score_answer`, so the tree would hold two judges reading
      different things, and every non-satisfied termination — `grader_error` included — returns the
      **ungraded** answer with only a log line. What would move this row is a measurement, not a
      library: revise the answers `score_answer` already flags and score the revisions. If revision
      helps, build the loop first-party on the one judge that reads the turn's own tool results.
      **Two things found while trying to run it.** The judge never ran at all until the
      `method="json_schema"` fix (every non-empty answer was flagged unconditionally, so the signal
      carried no information); and of six answers that completed, three were flagged for three
      different reasons, two at *high* judge confidence — including `promised but not called:
      screen_hazards`, which no rewrite of prose can fix, because the remedy is to call the tool.
      Treat the three flag reasons as three questions.

      **Measured, and the answer is no** (`D-2026-08-16-a-second-judge-…`): 51 probes on Haiku, 39
      flagged, 39 revised. Revision cleared 10 but **8 of the 10 were deletions**, and the 2 that
      kept their substance are exactly the number that clear with **no edit at all** when the judge
      re-rolls. Benefit over doing nothing: zero, at $0.0149 and 3.4 s median per flagged turn.
      `promised but not called` was "fixed" by deleting the promise 8 times out of 8. This row stays
      open only for the one experiment that could overturn it — a stronger judge *and* reviser on
      both legs — and the two rows below are prior to it.

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
