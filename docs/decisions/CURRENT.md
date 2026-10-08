# Decisions in force

The one-page index of what is decided **today**, grouped by area. One line each; the ADR holds the
reasoning. Where several ADRs touch a subject, the newest one in force is named. `README.md` is the
full ledger in record order; an ADR marked `**Superseded-by:**` has been replaced at least in part.

## Programme

- Knowledge graph moves to Postgres only, coordination is Postgres only, the agent stays on
  `deepagents` via public seams, tenancy is `tenant_id` + RLS, and the record is cut to current
  rules plus this page — [D-2026-10-07-the-architecture-programme](D-2026-10-07-the-architecture-programme.md).

## Layers and runtime

- Python is the runtime — [D-001](D-001-runtime-is-python.md).
- Layer 1 is a LangGraph graph compiled per turn by `create_deep_agent`, with a Postgres checkpointer — [D-2026-08-10-langgraph-rebuild-of-the-conversation-layer](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md).
- Durability lives only in Temporal; orchestration and durability stay separate — [D-002](D-002-maf-for-orchestration-temporal-for-durability-kept.md), [D-006](D-006-one-execution-system-temporal-task-queues-no-pg-boss.md).
- The harness is adopted whole and every default it brings is a decision made here — [D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently](D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently.md).
- The runaway cap is a first-party `before_model` counter, not upstream's `ModelCallLimitMiddleware` — [D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped](D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped.md).
- One gateway is the only LLM provider; nothing in `src/` dials a vendor — [D-2026-09-04-a-gateway-is-the-only-provider](D-2026-09-04-a-gateway-is-the-only-provider.md).
- `src/` is all the code; packages are grouped by layer and the map is enforced — [D-148](D-148-the-packages-regrouped-under-src-chemclaw-by-layer.md), [D-156](D-156-the-last-false-duplicate-and-a-map-that-is-enforced.md).
- An in-memory implementation is a test oracle unless a configuration selects it — [D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend](D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend.md).

## Agent, delegation and context

- A helper is an attenuation of its caller, not a new actor — [D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor](D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md), [D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller](D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller.md).
- A handoff redistributes the root's authority and cannot extend it; ships off — [D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it](D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it.md).
- Delegation did not pay on the measured gateway model; the surface stays as shipped — [D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model](D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model.md).
- Context budgets charge the request prefix up to `PREFIX_BOUND`; beyond it the excess is paid in spend — [D-2026-10-02-a-prefix-beyond-the-derivation-basis-is-paid-in-spend-not-thread](D-2026-10-02-a-prefix-beyond-the-derivation-basis-is-paid-in-spend-not-thread.md), [D-2026-10-03-the-fleet-narrowed-and-the-thread-and-the-cap-come-back](D-2026-10-03-the-fleet-narrowed-and-the-thread-and-the-cap-come-back.md).
- The per-turn spend cap is a runaway backstop; the per-user cap is a durable rolling window — [D-2026-08-29-an-iteration-cap-is-not-a-cost-cap](D-2026-08-29-an-iteration-cap-is-not-a-cost-cap.md), [D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota](D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota.md).
- A turn gets a filesystem (`/scratch`, `/skills` read-only, `/memories`) and no shell; code runs in the `pyexec` server — [D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work](D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work.md), [D-2026-08-25-a-sandbox-is-a-server-not-a-verb](D-2026-08-25-a-sandbox-is-a-server-not-a-verb.md), [D-2026-09-26-a-chemists-scratch-write-is-bounded-and-expires](D-2026-09-26-a-chemists-scratch-write-is-bounded-and-expires.md).
- Every out-of-process tool result is framed by a middleware as untrusted data — [D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so](D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so.md), [D-2026-09-06-the-envelope-covers-results-and-a-description-is-not-one](D-2026-09-06-the-envelope-covers-results-and-a-description-is-not-one.md).
- An unparseable or truncated tool call is an ordinary, visible tool failure — [D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure](D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure.md), [D-2026-09-25-a-call-cut-off-at-the-output-limit-does-not-run](D-2026-09-25-a-call-cut-off-at-the-output-limit-does-not-run.md).
- Artefacts are part of the answer, session-owned and versioned, not a gated effect — [D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect](D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect.md), whose decline of artefacts that run code is reversed by [D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves](D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves.md); model-written HTML runs its scripts by default in an opaque origin — [D-2026-10-03-model-written-html-runs-its-scripts-by-default](D-2026-10-03-model-written-html-runs-its-scripts-by-default.md).
- Templates are the plan, so a template step is read-only; fan-out is a composite — [D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only](D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only.md), [D-2026-08-25-the-loop-is-a-composite-not-a-template](D-2026-08-25-the-loop-is-a-composite-not-a-template.md).
- A chemist's standing preferences are pushed into every model call — [D-2026-10-02-standing-preferences-are-pushed-not-pulled](D-2026-10-02-standing-preferences-are-pushed-not-pulled.md).

## Knowledge, memory and retrieval

- Knowledge is written directly, labelled `created_by: agent`, and corrected rather than pre-approved — [D-2026-09-05-the-gate-follows-behaviour-not-knowledge](D-2026-09-05-the-gate-follows-behaviour-not-knowledge.md).
- No agent path writes a skill; a tier changes by its blast radius (`skills/` by commit, org by privileged route, a chemist's own by their route) — [D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius](D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius.md), [D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved](D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved.md), [D-2026-09-20-a-revert-is-a-pointer-when-there-is-no-commit-to-revert](D-2026-09-20-a-revert-is-a-pointer-when-there-is-no-commit-to-revert.md).
- Retrieval is hybrid and every chunk carries provenance — [D-062](D-062-f10-a-hybrid-retrieval-dense-lexical-entry-points.md), [D-160](D-160-retrieval-carries-provenance-so-a-claim-can-be.md).
- A vector is valid only for the model that made it; the vector store is not a catalogue — [D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it](D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it.md), [D-2026-08-08-a-vector-store-is-not-a-catalogue](D-2026-08-08-a-vector-store-is-not-a-catalogue.md).
- One canonical molecular identity and no second scheme — [D-2026-07-31-two-spellings-of-one-molecule](D-2026-07-31-two-spellings-of-one-molecule.md), [D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability](D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability.md).
- One generic fingerprint store, keyed by source — [D-017](D-017-one-generic-fingerprint-store-for-molecules-and.md), [D-2026-08-27-a-fingerprint-is-keyed-by-its-source](D-2026-08-27-a-fingerprint-is-keyed-by-its-source.md).
- Reaction labels are a derived index filled by a drain; the labelling models live in the fleet — [D-2026-08-25-a-label-is-derived-not-recorded](D-2026-08-25-a-label-is-derived-not-recorded.md), [D-2026-08-25-the-labeller-leaves-the-index-stays](D-2026-08-25-the-labeller-leaves-the-index-stays.md).
- No literature index until there is a corpus and a licence — [D-2026-09-27-a-literature-index-waits-for-a-corpus-and-a-licence](D-2026-09-27-a-literature-index-waits-for-a-corpus-and-a-licence.md).
- No external sources beyond what is vendored at build time — [D-089](D-089-no-external-sources-pdf-pptx-docx-xlsx-are-in-scope.md), [D-135](D-135-a-dataset-may-be-vendored-into-the-image-at-build.md).
- Unknown is not fine; trust travels with the value — [D-2026-08-01-unknown-is-not-fine](D-2026-08-01-unknown-is-not-fine.md), [D-169](D-169-trust-is-a-distribution-not-a-number-the-residual.md).

## Durable execution and calculation

- A persisted result is never recomputed — [D-011](D-011-results-are-persisted-once-never-recomputed.md); one being computed by another process is awaited, not recomputed — [D-2026-10-08-a-calculation-miss-is-claimed-in-postgres-before-it-is-computed](D-2026-10-08-a-calculation-miss-is-claimed-in-postgres-before-it-is-computed.md).
- The physics leaves for `Chemclaw3-mcp`, the cache stays; a composite is decomposed into keyed primitives — [D-2026-08-16-the-physics-leaves-the-cache-stays](D-2026-08-16-the-physics-leaves-the-cache-stays.md).
- Semiempirical is the whole tier: no HPC, no DFT — [D-2026-08-26-semiempirical-is-the-whole-tier](D-2026-08-26-semiempirical-is-the-whole-tier.md), [D-2026-09-19-the-condition-was-met-and-the-answer-is-still-no](D-2026-09-19-the-condition-was-met-and-the-answer-is-still-no.md).
- A cache is not a record: results are published to an external store through a sink — [D-2026-08-25-a-cache-is-not-a-record](D-2026-08-25-a-cache-is-not-a-record.md).
- A connector's job queue is derived from its name — [D-150](D-150-a-connector-jobs-task-queue-is-derived-not-declared.md); a heavy tool call waits in its interactive queue — [D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused](D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused.md).
- Every connector job leaves a durable record — [D-157](D-157-a-durable-record-of-every-connector-job-what-ran.md); a finished job reaches the model through the mailbox — [D-2026-08-27-the-mailbox-reaches-the-model-not-only-the-browser](D-2026-08-27-the-mailbox-reaches-the-model-not-only-the-browser.md).
- A geometry crosses into context as a handle, never a payload — [D-2026-08-21-a-geometry-is-an-address-not-a-payload](D-2026-08-21-a-geometry-is-an-address-not-a-payload.md).
- BoFire is the BO engine — [D-012](D-012-bofire-is-the-bayesian-optimization-engine-no-in.md), [D-2026-08-04-what-bofire-does-when-you-actually-run-it](D-2026-08-04-what-bofire-does-when-you-actually-run-it.md).

## Connectors, sources and sinks

- One connector seam: a directory with a `connector.yaml` — [D-118](D-118-one-connector-seam-for-mcp-temporal-and-long-running.md); a connector we do not run is an `endpoint:` — [D-2026-08-09-a-connector-we-do-not-run](D-2026-08-09-a-connector-we-do-not-run.md).
- A connector's manifest comes from one place, the fleet's pinned `chemclaw-contracts` package for the fleet's connectors, and a name declared in two directories is a startup error — [D-2026-10-08-a-connector-name-has-one-owner](D-2026-10-08-a-connector-name-has-one-owner.md).
- Declaring a capability and binding it into the prompt are separate decisions — [D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions](D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions.md).
- A data source is a manifest under `ingest/sources/` — [D-120](D-120-a-data-source-becomes-a-manifest-the-second-config.md); a `connection:` block is the driver's own keyword arguments — [D-2026-08-26-the-driver-s-signature-is-the-schema](D-2026-08-26-the-driver-s-signature-is-the-schema.md); a share is mounted, not called — [D-2026-08-06-a-share-is-mounted-not-called](D-2026-08-06-a-share-is-mounted-not-called.md).
- An ELN transcription is data, not a claim — [D-2026-08-25-an-eln-transcription-is-data-not-a-claim](D-2026-08-25-an-eln-transcription-is-data-not-a-claim.md); a feeder writes a table and nothing else — [D-2026-08-28-a-feeder-writes-a-table-and-nothing-else](D-2026-08-28-a-feeder-writes-a-table-and-nothing-else.md).
- Safety is an MCP tool, not a gate — [D-2026-08-15-safety-is-a-tool-not-a-gate](D-2026-08-15-safety-is-a-tool-not-a-gate.md).
- A claim about a sibling repository is checked by reading it — [D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it](D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it.md).

## Identity, authorization and security

- One per-tool authorization middleware; identity travels with the work — [D-060](D-060-f10-c-per-tool-authorization-middleware-supersedes-d.md), [D-2026-08-08-identity-must-travel-with-the-work](D-2026-08-08-identity-must-travel-with-the-work.md).
- One gate over one side-effecting set; an approval authorizes a request, not a session — [D-2026-07-31-one-gate-over-one-side-effecting-set](D-2026-07-31-one-gate-over-one-side-effecting-set.md), [D-167](D-167-an-approval-authorizes-a-request-not-a-session.md), [D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool](D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool.md).
- In a shared session the sender governs — [D-2026-09-27-in-a-shared-session-the-sender-governs](D-2026-09-27-in-a-shared-session-the-sender-governs.md).
- The audit trail is kept because it is useful; no GxP framing, no hash chain — [D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks](D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks.md).
- The conversation is erasable, the record is not — [D-2026-08-08-the-conversation-is-erasable-the-record-is-not](D-2026-08-08-the-conversation-is-erasable-the-record-is-not.md).
- Redaction outlives the formatter — [D-2026-08-08-redaction-must-outlive-the-formatter](D-2026-08-08-redaction-must-outlive-the-formatter.md).
- No egress: a compiled libc guard backs the Python one, and an ambient proxy is a destination — [D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py](D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py.md), [D-2026-09-12-an-ambient-proxy-is-a-destination-nobody-declared](D-2026-09-12-an-ambient-proxy-is-a-destination-nobody-declared.md).
- A model call is an OTLP span; LangSmith is declined — [D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment](D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment.md), [D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape](D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape.md).

## Front door, sessions and deployment

- The front door is a multi-process pure-ASGI service — [D-121](D-121-the-front-door-as-a-multi-process-service-pure-asgi.md); a running turn and an upload are reachable from any replica through Postgres — [D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica](D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica.md), [D-2026-10-04-an-upload-is-session-state-not-pod-state](D-2026-10-04-an-upload-is-session-state-not-pod-state.md).
- A turn is written ahead and an interrupted one says so — [D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so](D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so.md).
- One rootless image, one config source, a Helm chart on OpenShift — [D-049](D-049-f6-openshift-delivery-one-image-one-config-source.md); a release names its own Temporal namespace — [D-2026-09-09-a-temporal-namespace-is-a-releases-boundary-not-a-constant](D-2026-09-09-a-temporal-namespace-is-a-releases-boundary-not-a-constant.md).
- The schema only goes forward; rollback is not a schema step — [D-2026-08-04-the-schema-only-goes-forward](D-2026-08-04-the-schema-only-goes-forward.md), [D-2026-08-08-a-rollback-that-is-not-a-schema-step](D-2026-08-08-a-rollback-that-is-not-a-schema-step.md); the app is its own migrator — [D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns](D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns.md).
- The connection budget is a fleet number — [D-2026-08-05-the-connection-budget-is-a-fleet-number](D-2026-08-05-the-connection-budget-is-a-fleet-number.md).
- The background worker runs at any replica count: periodic jobs are Schedules under `SKIP`, and a pass that must not overlap itself takes a session-level advisory lock and skips when it is held — [D-2026-10-08-a-single-instance-job-holds-a-session-lock-or-does-nothing](D-2026-10-08-a-single-instance-job-holds-a-session-lock-or-does-nothing.md).
- The front door keeps three pools and a worker one; an early alert fires at 80% of the declared connection ceiling, and PgBouncer's transaction mode serves only connections with no session state — [D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint](D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint.md).
- The suite gate goes parallel; a parallel-only failure is a defect to root-cause, and until W3.12 flips the default the gate stays serial — [D-2026-10-08-the-test-gate-runs-in-parallel](D-2026-10-08-the-test-gate-runs-in-parallel.md).
- Model-facing text changes one batch at a time, behind an offline and a live A/B eval — [D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation](D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation.md).

## Record and process

- ADR ids are `D-YYYY-MM-DD-<slug>`; the `D-NNN` sequence is frozen — [D-2026-07-31-adr-ids-that-cannot-collide](D-2026-07-31-adr-ids-that-cannot-collide.md).
- An ADR is written for a choice or a decline, and a decline carries `Revisit when:` — [D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision](D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision.md).
- A backlog claim is a GitHub Issue, not a line edit — [D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit](D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit.md).
