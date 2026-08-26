# ADR index

One file per architecture decision — `D-YYYY-MM-DD-<slug>.md`, and `D-NNN-<slug>.md` for the frozen
numbered sequence — alongside this index. `docs/decisions/` holds the *reasoning*; this file is the
index of what exists, in record order.

**Why one file per ADR.** Until D-147 every decision was appended to the end of a single
`DECISIONS.md`. ADR numbers collided three times, and the cause was structural rather than
careless: concurrent branches all append to the same last line of the same file, and each picks
"the highest number I can see, plus one" against its own branch, which cannot see the others.
D-147 removed the shared append point: two branches adding different ADRs no longer touch the same
lines, and two branches claiming the *same* number collide on a **filename**, which git reports
loudly instead of burying it inside ninety lines of prose. That fixed the *detection* and left the
*allocation* alone, which is why the collisions continued — see below.

**Allocating an id.** Write the file as `D-YYYY-MM-DD-<slug>.md`, using today's date and a slug
that names the decision. That is the whole procedure — nothing to look up, nothing to reserve,
nothing to coordinate.

The id is the **whole stem**, not the date: two ADRs on one day is routine here, and an id naming
two decisions is exactly what this ledger exists to prevent. Collision therefore needs the same date
*and* the same slug, and even that arrives as an add/add conflict on a filename.

**Why the numbers stopped.** D-147 split one `DECISIONS.md` into one file per ADR so a collision
would be loud instead of buried, and that worked. What it could not fix is the allocation itself:
"highest on `origin/main`, plus one" is a read that goes stale the instant another session pushes,
and this repository runs many sessions at once. In one day, one branch renumbered three ADRs twice
and another renumbered three times — five collisions, every one of them on a number nobody had
merged yet. `CLAUDE.md` named this escape hatch and asked for it to be taken deliberately rather
than drifted into; D-2026-07-31 takes it.

**The `D-NNN` sequence is frozen, not migrated.** Every numbered ADR keeps its name, so every
citation to one keeps resolving. A *merged* ADR has never collided — only unallocated numbers were
ever contended, and there are no more of those. Both forms live in the one table below,
numbered first, then dated by date. A `RESERVED` row is legacy: it belongs to the numbered scheme
and is kept only for reservations that were in flight when this changed.

`tests/test_decision_log.py` enforces all of the above.

---

## By topic — where the current decision on a subject is

The table at the bottom is the *record*: every ADR, in the order it was written. It is not a way to
find out what is true. With 300+ entries, "what does this system currently decide about retrieval?"
was answerable only by reading every title and guessing, and a reader who guessed wrong landed on a
decision two ADRs had since replaced.

This table is the reader's entry point. One row per subject that has **more than one** ADR; the
middle column is what to read now, the right column is what that decision absorbed, replaced or
amended. A subject with exactly one ADR is not listed — the record's own table finds it.

**A row here never overrides an ADR's own `Supersedes` line.** Where an ADR states its supersessions
(D-2026-08-14 and D-2026-08-15 both do), that statement is authoritative and this table is a pointer
to it. Where it does not, the right column records the reading a maintainer arrived at, and the
older ADR is still *correct about the moment it was written* — that is the whole reason it is kept.

| Topic | Read this now | Earlier ADRs it supersedes, absorbs or amends |
|---|---|---|
| Layer-1 orchestration framework | [D-2026-08-10-langgraph-rebuild-of-the-conversation-layer](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md), amended by [D-2026-08-14-the-coupling-is-the-cost-not-the-line-count](D-2026-08-14-the-coupling-is-the-cost-not-the-line-count.md) | D-002, D-007, D-013, D-038, D-040, D-123 (all MAF-era), D-2026-08-11-what-the-removal-found |
| Delegation, subagents, challenge panel | [D-2026-08-15-a-capability-that-ships-off-is-not-a-capability](D-2026-08-15-a-capability-that-ships-off-is-not-a-capability.md) | D-064, D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate, D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks, D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared. **Not** D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor — its invariants still bind any future subagent |
| Specialist observability | [D-2026-08-11-the-specialists-name-is-not-in-the-namespace](D-2026-08-11-the-specialists-name-is-not-in-the-namespace.md) | D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs; both are moot while no team ships (see the row above) |
| The audit trail | [D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks](D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks.md) | D-027, D-034, D-055, D-061, D-122, D-2026-07-31-the-audit-chain-is-versioned, D-2026-08-01-a-restore-is-a-truncation-nobody-can-see. D-2026-08-05-append-only-by-grant-not-by-contract is explicitly **left standing** |
| Context compaction | [D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has](D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has.md), corrected by [D-2026-08-11-what-the-review-found-in-the-compaction-change](D-2026-08-11-what-the-review-found-in-the-compaction-change.md) | D-025 (MAF compaction, mechanism removed with the framework), D-151 |
| Capability seam (connectors) | [D-118](D-118-one-connector-seam-for-mcp-temporal-and-long-running.md), with [D-150](D-150-a-connector-jobs-task-queue-is-derived-not-declared.md) on queues | D-016, D-029, D-103, D-110, D-111, D-112, D-115 |
| Data-source / ingestion seam | [D-120](D-120-a-data-source-becomes-a-manifest-the-second-config.md), extended by [D-2026-08-04-the-schema-is-a-file](D-2026-08-04-the-schema-is-a-file.md) and [D-2026-08-06-a-share-is-mounted-not-called](D-2026-08-06-a-share-is-mounted-not-called.md) | D-018, D-022, D-028, D-050, D-053, D-076 |
| Retrieval | [D-062](D-062-f10-a-hybrid-retrieval-dense-lexical-entry-points.md) (hybrid) with [D-2026-08-13-both-lexical-backends-state-one-boolean-rule](D-2026-08-13-both-lexical-backends-state-one-boolean-rule.md) and [D-2026-08-01-a-cap-that-starves-a-source](D-2026-08-01-a-cap-that-starves-a-source.md) | D-008, D-020, D-055, D-056, D-057, D-160 |
| Vector store & embedding identity | [D-2026-08-08-a-vector-store-is-not-a-catalogue](D-2026-08-08-a-vector-store-is-not-a-catalogue.md) with [D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it](D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it.md) and [D-2026-08-08-a-derived-index-must-record-what-derived-it](D-2026-08-08-a-derived-index-must-record-what-derived-it.md) | D-2026-08-13-analyze-first-and-the-recall-knobs-second is the tuning residual, not a replacement |
| Bayesian optimization | [D-2026-08-04-what-bofire-does-when-you-actually-run-it](D-2026-08-04-what-bofire-does-when-you-actually-run-it.md) | D-012, D-096, D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate, D-2026-08-05-a-ceiling-that-does-not-hold, D-2026-08-08-a-category-has-no-outside, D-2026-07-31-a-campaign-is-an-entity-not-a-turn |
| Chemical safety screen | [D-080](D-080-chemical-safety-a-deterministic-advisory-structural.md) with [D-2026-08-06-a-pair-rule-is-a-cross-product](D-2026-08-06-a-pair-rule-is-a-cross-product.md) and [D-2026-08-09-a-valid-prefix-is-not-a-molecule](D-2026-08-09-a-valid-prefix-is-not-a-molecule.md) | D-090 |
| Molecular identity / canonical SMILES | [D-2026-07-31-two-spellings-of-one-molecule](D-2026-07-31-two-spellings-of-one-molecule.md) with [D-2026-08-01-a-reagent-is-not-its-largest-fragment](D-2026-08-01-a-reagent-is-not-its-largest-fragment.md) | D-033 |
| Calculation cache & keys | [D-011](D-011-results-are-persisted-once-never-recomputed.md), keyed as [D-2026-08-01-a-key-names-what-ran](D-2026-08-01-a-key-names-what-ran.md) / [D-2026-08-01-a-key-that-cannot-see-our-own-fix](D-2026-08-01-a-key-that-cannot-see-our-own-fix.md) | D-015, D-158 |
| Identity & authorization | [D-060](D-060-f10-c-per-tool-authorization-middleware-supersedes-d.md) with [D-2026-08-08-identity-must-travel-with-the-work](D-2026-08-08-identity-must-travel-with-the-work.md) and [D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim](D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim.md) | D-043, D-044 (scope superseded by D-060), D-045, D-046, D-047, D-052, D-068 |
| Approvals / human-in-the-loop | [D-167](D-167-an-approval-authorizes-a-request-not-a-session.md) with [D-137](D-137-the-plan-the-model-could-approve-for-itself-a-pre.md) and [D-2026-07-31-one-gate-over-one-side-effecting-set](D-2026-07-31-one-gate-over-one-side-effecting-set.md) | D-005 (the principle), D-032, D-035 |
| PR-gate & the note vocabulary | [D-161](D-161-the-human-gate-moves-from-every-observation-to.md) with [D-2026-08-08-a-bundle-may-extend-a-closed-vocabulary](D-2026-08-08-a-bundle-may-extend-a-closed-vocabulary.md), [D-133](D-133-a-submission-is-a-note-and-what-it-needs-so-a.md), [D-134](D-134-edges-carry-relations-and-their-own-validity-so-the.md) | D-004, D-005, D-021, D-074, D-164, D-2026-07-31-a-proposal-is-a-record-not-a-branch |
| Durable execution & queues | [D-150](D-150-a-connector-jobs-task-queue-is-derived-not-declared.md) with [D-099](D-099-durable-capabilities-declare-their-own-queue.md) | D-006, D-002's durability half |
| Durable job records | [D-157](D-157-a-durable-record-of-every-connector-job-what-ran.md) | D-2026-08-08-an-outage-is-not-a-missing-job refines its failure reporting |
| HPC / QM execution | [D-048](D-048-f5-real-hpc-execution-via-a-nextflow-launcher-behind.md) with [D-098](D-098-x3-x4-geometries-free-energies-the-reaction.md), [D-101](D-101-x5-x6-x7-the-binaries-and-what-they-change.md), [D-2026-08-01-one-equilibrium-or-no-number](D-2026-08-01-one-equilibrium-or-no-number.md) | D-010, D-095, D-097, D-100, D-102, D-104, D-108, D-132 |
| Uncertainty & trust in a number | [D-2026-08-01-unknown-is-not-fine](D-2026-08-01-unknown-is-not-fine.md) with [D-2026-08-01-trust-travels-on-the-value-line](D-2026-08-01-trust-travels-on-the-value-line.md) and [D-169](D-169-trust-is-a-distribution-not-a-number-the-residual.md) | D-170 applies the same rule to similarity hits |
| Front door / SSE contract | [D-121](D-121-the-front-door-as-a-multi-process-service-pure-asgi.md) with [D-159](D-159-the-turn-stream-reports-a-tool-s-lifecycle-not.md) and [D-166](D-166-the-queue-is-reported-on-the-stream-not-as-a.md) | D-041, D-077, D-2026-08-01-a-turn-you-can-follow-across-a-process |
| Sessions & turn state | [D-042](D-042-f3-durable-session-job-session-push-back-foundation.md) with [D-2026-08-13-a-checkpoint-says-which-schema-wrote-it](D-2026-08-13-a-checkpoint-says-which-schema-wrote-it.md) and [D-2026-08-11-what-the-removal-found](D-2026-08-11-what-the-removal-found.md) | D-145, D-2026-08-10-a-list-of-ids-is-not-a-conversation-list |
| Observability / tracing | [D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment](D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment.md) with [D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape](D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape.md) | D-026, D-027's OTel half, D-152, D-2026-08-03-a-metric-must-declare-what-it-can-see |
| Cost & token budget | [D-144](D-144-token-accounting-was-priced-blind-one-total-where.md) with [D-2026-08-01-spend-is-a-ledger-not-a-label](D-2026-08-01-spend-is-a-ledger-not-a-label.md) and [D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it](D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it.md) | D-025, D-066's budget half, D-2026-08-12-the-cache-floor-is-per-model-and-two-profiles-are-under-it |
| Eval & metric layer | [D-009](D-009-evaluation-metrics-layer-is-first-class-phase-2b.md) with [D-063](D-063-f10-f-classification-metrics-p-r-f1-eval-drift.md), [D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet](D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet.md), [D-2026-08-12-the-experiment-surface-is-the-record-somebody-can-open](D-2026-08-12-the-experiment-surface-is-the-record-somebody-can-open.md) | D-014, D-056 |
| Migrations & schema evolution | [D-2026-08-04-the-schema-only-goes-forward](D-2026-08-04-the-schema-only-goes-forward.md) with [D-2026-08-08-a-rollback-that-is-not-a-schema-step](D-2026-08-08-a-rollback-that-is-not-a-schema-step.md) and [D-2026-08-01-a-migration-waits-in-front-of-live-traffic](D-2026-08-01-a-migration-waits-in-front-of-live-traffic.md) | D-034's ledger half, D-149 |
| Retention & erasure | [D-2026-08-08-the-conversation-is-erasable-the-record-is-not](D-2026-08-08-the-conversation-is-erasable-the-record-is-not.md) with [D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim](D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim.md) | D-145; the checkpoint tables joined it in D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has |
| Deployment / OpenShift | [D-049](D-049-f6-openshift-delivery-one-image-one-config-source.md) with [D-2026-07-31-the-deployment-envelope](D-2026-07-31-the-deployment-envelope.md) and [D-2026-08-01-a-tag-is-a-pointer-not-a-build](D-2026-08-01-a-tag-is-a-pointer-not-a-build.md) | D-082's chart half, D-135 |
| Secrets & log redaction | [D-2026-08-08-redaction-must-outlive-the-formatter](D-2026-08-08-redaction-must-outlive-the-formatter.md) with [D-2026-08-06-a-redactor-that-only-reads-the-message](D-2026-08-06-a-redactor-that-only-reads-the-message.md) and [D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not](D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not.md) | D-026's logging floor |
| Templates (fixed procedures) | [D-113](D-113-stages-d-and-e-profiles-select-an-agent-templates.md) with [D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only](D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only.md), [D-140](D-140-a-template-s-job-step-resolved-off-the-workflow.md), [D-168](D-168-a-template-step-runs-as-its-requester.md) | — |
| Skills (layer 3) | [D-003](D-003-agent-skills-skill-md-for-capability-integration.md) with [D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches](D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches.md) and [D-052](D-052-role-scoped-skill-visibility-salvaged-from-the.md) | the loading mechanism moved to `deepagents` in D-2026-08-10-langgraph-rebuild-of-the-conversation-layer |
| Memory layers | [D-019](D-019-memory-layers-add-no-new-infrastructure-note-types.md) with [D-078](D-078-memory-notes-are-retired-when-their-cluster-merges.md) and [D-2026-08-10-basestore-is-not-where-this-systems-memory-lives](D-2026-08-10-basestore-is-not-where-this-systems-memory-lives.md) | — |
| The ADR record itself | [D-2026-07-31-adr-ids-that-cannot-collide](D-2026-07-31-adr-ids-that-cannot-collide.md) | D-088, D-147 (detection, not allocation), D-109's ledger half |
| Prose & declaration gates | [D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check](D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check.md) with [D-164](D-164-the-prose-gate-learns-note-types-and-the-two-dead.md), [D-149](D-149-what-two-finished-migrations-left-behind-and-a-guard.md), [D-2026-08-08-a-rule-with-no-test-is-a-claim](D-2026-08-08-a-rule-with-no-test-is-a-claim.md) | — |
| Repository layout | [D-156](D-156-the-last-false-duplicate-and-a-map-that-is-enforced.md) with [D-148](D-148-the-packages-regrouped-under-src-chemclaw-by-layer.md) and [D-2026-08-02-the-seam-does-not-move](D-2026-08-02-the-seam-does-not-move.md) | D-091, D-117, D-146 |
| Pools, connections, scale | [D-119](D-119-production-scale-the-event-loop-the-connection-pool.md) with [D-2026-08-05-the-connection-budget-is-a-fleet-number](D-2026-08-05-the-connection-budget-is-a-fleet-number.md), [D-2026-08-05-a-worker-may-not-outrun-its-pool](D-2026-08-05-a-worker-may-not-outrun-its-pool.md), [D-2026-08-08-a-borrowed-connection-is-bounded-by-default](D-2026-08-08-a-borrowed-connection-is-bounded-by-default.md) | D-066's clamp half |
| LLM provider seam & model routing | [D-039](D-039-f0-config-selected-llm-provider-seam-foundation-plan.md) with [D-059](D-059-f10-e-b-per-task-model-routing-answer-verification.md) and [D-2026-08-12-the-cache-floor-is-per-model-and-two-profiles-are-under-it](D-2026-08-12-the-cache-floor-is-per-model-and-two-profiles-are-under-it.md) | — |
| Fingerprint search | [D-017](D-017-one-generic-fingerprint-store-for-molecules-and.md) | D-029 (the MCP consumption half now runs through D-118), D-031 |
| ELN entries & versioning | [D-2026-07-31-an-eln-entry-is-versioned-not-immutable](D-2026-07-31-an-eln-entry-is-versioned-not-immutable.md) with [D-2026-08-04-the-schema-is-a-file](D-2026-08-04-the-schema-is-a-file.md) | D-018, D-022, D-054, D-070 |
| Scope of external data | [D-089](D-089-no-external-sources-pdf-pptx-docx-xlsx-are-in-scope.md), amended once by [D-135](D-135-a-dataset-may-be-vendored-into-the-image-at-build.md) | — |
| Degradation & failure reporting | [D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed](D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed.md) with [D-2026-08-08-a-degraded-check-must-not-clear-the-gate](D-2026-08-08-a-degraded-check-must-not-clear-the-gate.md) and [D-2026-08-08-a-partial-answer-must-say-so](D-2026-08-08-a-partial-answer-must-say-so.md) | D-139 |

## Where the record still says "GxP"

**The regulatory framing is not a live constraint.** `D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks`
removed it: `grep -r "GxP\|21 CFR\|ALCOA\|GAMP" src/ tests/` returns **zero** matches today. What it
deliberately did *not* do is rewrite the record — "`docs/decisions/` and `docs/archive/` are not
rewritten. A merged ADR is never edited (CLAUDE.md) and an archived document is a record of what was
true then; this ADR is what supersedes them."

That leaves **60 ADR files (~6,500 lines, a quarter of this corpus) whose prose still uses a
vocabulary the system dropped**, with nothing inside them saying so. This section is the marker.
It lives here rather than as a banner inside each file for the reason the superseding ADR gives: a
merged ADR is never edited, and this index is the mechanism that decision itself names.

**Read every "GxP" below as "the tool-audit trail", every "21 CFR Part 11" as an argument about why
a record is worth keeping, and every claim of a validated posture as withdrawn.** The evals say so
in the running system: `data/evals/probes/knowledge.yaml` kn-29 grades the agent on *refusing* to
claim validated status, and `reporting.yaml` rp-13 forbids claiming 21 CFR Part 11 compliance.

**Superseded in substance — the mechanism is gone, not just the wording.** D-2026-08-14 names each
of these:

- [D-027](D-027-gxp-tool-audit-middleware-opt-in-opentelemetry-maf.md) · [D-055](D-055-gxp-freshness-read-time-provenance-in-graph.md) · [D-122](D-122-the-gxp-audit-trail-defaults-to-durable-because.md) — framing superseded; the trail, the freshness filter and the durable default all still run.
- [D-034](D-034-review-hardening-migration-ledger-durable-audit.md) · [D-061](D-061-f10-g-audit-hash-chain-bi-temporal-note-fields-d-a15.md) · [D-2026-07-31-the-audit-chain-is-versioned](D-2026-07-31-the-audit-chain-is-versioned.md) · [D-2026-08-01-a-restore-is-a-truncation-nobody-can-see](D-2026-08-01-a-restore-is-a-truncation-nobody-can-see.md) — the hash chain and its anchors these built are **deleted**. Everything they describe about verification, `make audit-verify`, `audit_anchor_secret` or a chain tip no longer exists.
- [D-2026-08-05-append-only-by-grant-not-by-contract](D-2026-08-05-append-only-by-grant-not-by-contract.md) is the exception: D-2026-08-14 leaves it standing as **the whole** of the integrity claim.

**Vocabulary only — the decision stands, one or two sentences in it use the dropped words.** These
51 need no re-reading beyond the substitution above:

[D-031](D-031-deep-review-deferred-items-worked-off-fp-definition.md) ·
[D-032](D-032-durable-async-approval-hold-for-captured-user.md) ·
[D-040](D-040-f1-maf-agent-harness-is-the-autonomous-plan-execute.md) ·
[D-065](D-065-f10-post-implementation-review-cycle-verified-fixes.md) ·
[D-082](D-082-graph-cache-ttl-da-5-decision-d-1-and-the-helm.md) ·
[D-083](D-083-f11-waves-0-3-closing-the-capability-gaps-deployment.md) ·
[D-109](D-109-four-fixes-from-the-live-e2e-pass-and-two-root.md) ·
[D-112](D-112-bo-as-the-reference-connector-owned-durable.md) ·
[D-118](D-118-one-connector-seam-for-mcp-temporal-and-long-running.md) ·
[D-119](D-119-production-scale-the-event-loop-the-connection-pool.md) ·
[D-131](D-131-the-connector-health-probe-follows-the-address.md) ·
[D-133](D-133-a-submission-is-a-note-and-what-it-needs-so-a.md) ·
[D-137](D-137-the-plan-the-model-could-approve-for-itself-a-pre.md) ·
[D-140](D-140-a-template-s-job-step-resolved-off-the-workflow.md) ·
[D-141](D-141-two-facts-that-stopped-at-a-process-boundary-a.md) ·
[D-155](D-155-what-the-dark-half-of-the-system-does-the-first-time.md) ·
[D-158](D-158-the-expensive-calculation-is-the-one-that-was-not.md) ·
[D-167](D-167-an-approval-authorizes-a-request-not-a-session.md) ·
[D-168](D-168-a-template-step-runs-as-its-requester.md) ·
[D-2026-07-31-a-proposal-is-a-record-not-a-branch](D-2026-07-31-a-proposal-is-a-record-not-a-branch.md) ·
[D-2026-07-31-the-deployment-envelope](D-2026-07-31-the-deployment-envelope.md) ·
[D-2026-08-01-a-cap-that-starves-a-source](D-2026-08-01-a-cap-that-starves-a-source.md) ·
[D-2026-08-01-a-declaration-that-authorizes-nothing](D-2026-08-01-a-declaration-that-authorizes-nothing.md) ·
[D-2026-08-01-a-gate-that-leaks-on-the-failure-path](D-2026-08-01-a-gate-that-leaks-on-the-failure-path.md) ·
[D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not](D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not.md) ·
[D-2026-08-01-every-process-carries-its-own-witness](D-2026-08-01-every-process-carries-its-own-witness.md) ·
[D-2026-08-01-spend-is-a-ledger-not-a-label](D-2026-08-01-spend-is-a-ledger-not-a-label.md) ·
[D-2026-08-01-trust-travels-on-the-value-line](D-2026-08-01-trust-travels-on-the-value-line.md) ·
[D-2026-08-01-unknown-is-not-fine](D-2026-08-01-unknown-is-not-fine.md) ·
[D-2026-08-04-the-schema-only-goes-forward](D-2026-08-04-the-schema-only-goes-forward.md) ·
[D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without](D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without.md) ·
[D-2026-08-06-a-gate-that-names-nothing](D-2026-08-06-a-gate-that-names-nothing.md) ·
[D-2026-08-06-a-redactor-that-only-reads-the-message](D-2026-08-06-a-redactor-that-only-reads-the-message.md) ·
[D-2026-08-06-a-share-is-mounted-not-called](D-2026-08-06-a-share-is-mounted-not-called.md) ·
[D-2026-08-08-a-rule-with-no-test-is-a-claim](D-2026-08-08-a-rule-with-no-test-is-a-claim.md) ·
[D-2026-08-08-a-survivor-is-a-hypothesis](D-2026-08-08-a-survivor-is-a-hypothesis.md) ·
[D-2026-08-08-an-outage-is-not-a-missing-job](D-2026-08-08-an-outage-is-not-a-missing-job.md) ·
[D-2026-08-08-identity-must-travel-with-the-work](D-2026-08-08-identity-must-travel-with-the-work.md) ·
[D-2026-08-08-the-conversation-is-erasable-the-record-is-not](D-2026-08-08-the-conversation-is-erasable-the-record-is-not.md) ·
[D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one](D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one.md) ·
[D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor](D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md) ·
[D-2026-08-10-basestore-is-not-where-this-systems-memory-lives](D-2026-08-10-basestore-is-not-where-this-systems-memory-lives.md) ·
[D-2026-08-10-langgraph-rebuild-of-the-conversation-layer](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md) ·
[D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs](D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs.md) ·
[D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has](D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has.md) ·
[D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate](D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate.md) ·
[D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape](D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape.md) ·
[D-2026-08-11-what-the-removal-found](D-2026-08-11-what-the-removal-found.md) ·
[D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks](D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks.md) ·
[D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim](D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim.md) ·
[D-2026-08-14-the-coupling-is-the-cost-not-the-line-count](D-2026-08-14-the-coupling-is-the-cost-not-the-line-count.md)

## Every decision, in record order

This is the ledger the tests enforce: exactly the files beside it, in the order `_sort_key` defines.

| ADR | Title |
|---|---|
| [D-001](D-001-runtime-is-python.md) | Runtime is Python |
| [D-002](D-002-maf-for-orchestration-temporal-for-durability-kept.md) | MAF for orchestration, Temporal for durability (kept separate) |
| [D-003](D-003-agent-skills-skill-md-for-capability-integration.md) | Agent Skills (SKILL.md) for capability integration |
| [D-004](D-004-knowledge-as-a-markdown-git-graph-networkx-not-a.md) | Knowledge as a Markdown + Git graph (NetworkX), not a graph DB |
| [D-005](D-005-human-in-the-loop-via-pr-gate.md) | Human-in-the-loop via PR-gate |
| [D-006](D-006-one-execution-system-temporal-task-queues-no-pg-boss.md) | One execution system: Temporal task queues, no pg-boss |
| [D-007](D-007-first-milestone-maf-temporal-spine-hpc-mocked.md) | First milestone: MAF + Temporal spine (HPC mocked) |
| [D-008](D-008-deep-research-report-harness-one-core-pluggable.md) | Deep-research/report harness: one core, pluggable retrievers |
| [D-009](D-009-evaluation-metrics-layer-is-first-class-phase-2b.md) | Evaluation/metrics layer is first-class (Phase 2b) |
| [D-010](D-010-hpc-dft-deferred-lead-with-fast-local-calculators.md) | HPC/DFT deferred; lead with fast local calculators (user decision) |
| [D-011](D-011-results-are-persisted-once-never-recomputed.md) | Results are persisted once, never recomputed (calculation store, first-class) |
| [D-012](D-012-bofire-is-the-bayesian-optimization-engine-no-in.md) | BoFire is the Bayesian-optimization engine (no in-house BO), pulled forward |
| [D-013](D-013-maf-stays-the-orchestrator-reaffirmed-vs-langgraph.md) | MAF stays the orchestrator (reaffirmed vs. LangGraph) |
| [D-014](D-014-eval-cases-live-outside-the-knowledge-graph-own.md) | Eval cases live outside the knowledge graph (own versioned dir, not notes) |
| [D-015](D-015-calculator-contract-now-run-cached-name-registry.md) | Calculator contract now (`run_cached`), name-registry deferred |
| [D-016](D-016-mcp-capability-servers-live-in-mcp-servers-not-mcp.md) | MCP capability servers live in `mcp_servers/`, not `mcp/` |
| [D-017](D-017-one-generic-fingerprint-store-for-molecules-and.md) | One generic fingerprint store for molecules and reactions |
| [D-018](D-018-eln-ingestion-ord-subset-schema-one-json-adapter-llm.md) | ELN ingestion: ORD-subset schema, one JSON adapter, LLM-per-field deferred |
| [D-019](D-019-memory-layers-add-no-new-infrastructure-note-types.md) | Memory layers add no new infrastructure (note types + jobs only) |
| [D-020](D-020-report-harness-reuses-retrievers-over-existing-data.md) | Report harness reuses retrievers over existing data (no new store) |
| [D-021](D-021-production-readiness-review-one-bad-data-contract.md) | Production-readiness review: one bad-data contract, hardened PR-gate |
| [D-022](D-022-eln-carries-step-by-step-recipes-a-second-adapter.md) | ELN carries step-by-step recipes; a second adapter reads native ORD |
| [D-023](D-023-the-agent-is-the-research-surface-integrations-stay.md) | The agent is the research surface; integrations stay dumb |
| [D-024](D-024-the-agent-computes-and-designs-experiments.md) | The agent computes and designs experiments proactively, not just retrieves |
| [D-025](D-025-the-agent-keeps-its-chat-thread-within-a-token.md) | The agent keeps its chat thread within a token budget (MAF compaction) |
| [D-026](D-026-observability-floor-config-driven-logging-one-clear.md) | Observability floor: config-driven logging + one clear DB-connect failure |
| [D-027](D-027-gxp-tool-audit-middleware-opt-in-opentelemetry-maf.md) | GxP tool-audit middleware + opt-in OpenTelemetry (MAF out-of-the-box) |
| [D-028](D-028-admin-pluggability-eln-adapter-registry-multi-dir.md) | Admin pluggability: ELN adapter registry, multi-dir skills, cache-trace log |
| [D-029](D-029-the-agent-consumes-fingerprint-search-over-mcp.md) | The agent consumes fingerprint search over MCP (config-driven servers) |
| [D-030](D-030-deep-review-hardening-bounded-retries-git-ref-safe.md) | Deep-review hardening: bounded retries, git-ref-safe slugs, git timeouts, cache keys |
| [D-031](D-031-deep-review-deferred-items-worked-off-fp-definition.md) | Deep-review deferred items worked off: fp-definition guard, ELN re-drive, KISS cleanups |
| [D-032](D-032-durable-async-approval-hold-for-captured-user.md) | Durable async approval hold for captured user answers (Yes/No button seam) |
| [D-033](D-033-one-canonical-identity-scheme-sha-256-hashing.md) | One canonical identity scheme: SHA-256 hashing + canonical SMILES in every key |
| [D-034](D-034-review-hardening-migration-ledger-durable-audit.md) | Review hardening: migration ledger, durable audit trail, injection framing, stmt timeout |
| [D-035](D-035-missing-runnable-seams-schedules-eln-cursor.md) | Missing runnable seams: schedules, ELN cursor persistence, approval + skill-role seams |
| [D-036](D-036-review-cleanup-dedupe-name-drift-guard-neutral.md) | Review cleanup: dedupe, name-drift guard, neutral config names, doc refresh |
| [D-037](D-037-tooling-gaps-coverage-unified-mypy-scope-worker.md) | Tooling gaps: coverage, unified mypy scope, worker tests, preflight, skill-validate |
| [D-038](D-038-maf-agent-harness-as-an-optional-third-reasoning.md) | MAF Agent Harness as an optional third reasoning backbone |
| [D-039](D-039-f0-config-selected-llm-provider-seam-foundation-plan.md) | F0: config-selected LLM provider seam (foundation-plan D-A1) |
| [D-040](D-040-f1-maf-agent-harness-is-the-autonomous-plan-execute.md) | F1: MAF Agent Harness is the autonomous plan/execute backbone (foundation D-020) |
| [D-041](D-041-f2-front-door-run-service-foundation-plan-d-a2.md) | F2: front-door run service (foundation-plan D-A2) |
| [D-042](D-042-f3-durable-session-job-session-push-back-foundation.md) | F3: durable session + job→session push-back (foundation-plan D-A3) |
| [D-043](D-043-f4-entra-id-identity-rbac-front-door-oidc-one.md) | F4: Entra ID identity & RBAC — front-door OIDC + one authorization gate (D-A4) |
| [D-044](D-044-f4-t3-the-core-rule-user-triggered-workflows-are.md) | F4-T3: the core rule — user-triggered workflows are user-specific via `require_actor` |
| [D-045](D-045-f4-t2-workload-identity-federation-a-pod-mints-its.md) | F4-T2: workload identity federation (a pod mints its own token, no secret at rest) |
| [D-046](D-046-f4-t4-on-behalf-of-exchange-for-user-scoped.md) | F4-T4: On-Behalf-Of exchange for user-scoped downstream (wired, dormant) |
| [D-047](D-047-f4-t6-the-two-non-entra-transport-bridges-carry.md) | F4-T6: the two non-Entra transport bridges carry identity as a claim |
| [D-048](D-048-f5-real-hpc-execution-via-a-nextflow-launcher-behind.md) | F5: real HPC execution via a Nextflow launcher behind the QM activities (D-A5, D-A5a) |
| [D-049](D-049-f6-openshift-delivery-one-image-one-config-source.md) | F6: OpenShift delivery — one image, one config source, three plain secrets (D-A6, D-A6a) |
| [D-050](D-050-f7-the-generic-data-source-seam-compose-two-half.md) | F7: the generic data-source seam (compose two half-contracts, don't merge them) |
| [D-051](D-051-foundation-review-f4-f7-adversarial-review-fixes.md) | Foundation review (F4–F7): adversarial review + fixes |
| [D-052](D-052-role-scoped-skill-visibility-salvaged-from-the.md) | Role-scoped skill visibility (salvaged from the phase6-authz branch) |
| [D-053](D-053-consolidate-eln-source-selection-onto-the-f7-seam.md) | Consolidate ELN source selection onto the F7 seam; memory honors `data_sources` (audit DUP-1) |
| [D-054](D-054-per-source-eln-cursors-a-per-scope-token-lock-close.md) | Per-source ELN cursors + a per-scope token lock (close the two F-review deferrals) |
| [D-055](D-055-gxp-freshness-read-time-provenance-in-graph.md) | GxP freshness + read-time provenance in graph retrieval (audit KM-6, KM-7) |
| [D-056](D-056-retrieval-quality-gate-a-starter-gold-set-registered.md) | Retrieval-quality gate: a starter gold set + registered metrics (audit KM-13) |
| [D-057](D-057-four-more-engine-gaps-closed-km-5-km-14-retrieval.md) | Four more engine gaps closed (KM-5, KM-14 retrieval half, AG-14, AG-15) |
| [D-058](D-058-prove-the-harness-loop-live-close-the-f3-t3-awaiting.md) | Prove the harness loop live; close the F3-T3 awaiting-todo deferral |
| [D-059](D-059-f10-e-b-per-task-model-routing-answer-verification.md) | F10-E/B: per-task model routing + answer verification & confidence routing (D-A11) |
| [D-060](D-060-f10-c-per-tool-authorization-middleware-supersedes-d.md) | F10-C: per-tool authorization middleware (supersedes D-044 scope, D-A12) |
| [D-061](D-061-f10-g-audit-hash-chain-bi-temporal-note-fields-d-a15.md) | F10-G: audit hash-chain + bi-temporal note fields (D-A15) |
| [D-062](D-062-f10-a-hybrid-retrieval-dense-lexical-entry-points.md) | F10-A: hybrid retrieval — dense + lexical entry points, RRF fusion (D-A10) |
| [D-063](D-063-f10-f-classification-metrics-p-r-f1-eval-drift.md) | F10-F: classification metrics (P/R/F1) + eval drift detection (D-A14) |
| [D-064](D-064-f10-d-sub-agent-orchestration-via-temporal-child.md) | F10-D: sub-agent orchestration via Temporal child workflows (D-A13) |
| [D-065](D-065-f10-post-implementation-review-cycle-verified-fixes.md) | F10 post-implementation review cycle: verified fixes |
| [D-066](D-066-resilience-hardening-db-query-clamps-session.md) | Resilience hardening: DB-query clamps, session reattach, turn/token budgets |
| [D-067](D-067-fail-closed-startup-unauthenticated-network-exposed.md) | Fail-closed startup: unauthenticated + network-exposed refuses to boot |
| [D-068](D-068-write-tools-are-role-gated-by-default-default-write.md) | Write tools are role-gated by default (DEFAULT_WRITE_TOOL_GATES) |
| [D-069](D-069-submitter-checkout-ownership-enforced-with-an-os.md) | Submitter checkout ownership enforced with an OS-level advisory lock |
| [D-070](D-070-eln-sync-cursor-semantics-future-tolerance-clamp.md) | ELN sync cursor semantics: future-tolerance clamp, overlap window, chunked activities |
| [D-071](D-071-deterministic-config-capture-in-workflows-idempotent.md) | Deterministic config capture in workflows; idempotent session events |
| [D-072](D-072-checkmate-campaign-2026-07-adversarially-verified.md) | CHECKMATE campaign 2026-07: adversarially-verified review, hardening, and refactor pass |
| [D-073](D-073-final-adversarial-diff-pass-campaign-introduced.md) | Final adversarial diff pass: campaign-introduced defects caught and fixed |
| [D-074](D-074-compared-against-google-s-open-knowledge-format-okf.md) | Compared against Google's Open Knowledge Format (OKF v0.1): design reaffirmed, two follow-ups queued |
| [D-075](D-075-config-extensibility-tool-registry-agentprofile-seam.md) | Config-extensibility: `@tool` registry + `AgentProfile` seam (audit doc 10, items 2–3) |
| [D-076](D-076-config-extensibility-datasourcespec-discriminated.md) | Config-extensibility: `DataSourceSpec` discriminated union (audit doc 10, item 4) |
| [D-077](D-077-the-turn-stream-emits-its-plan-and-its-job-launches.md) | The turn stream emits its plan and its job launches (F2/F3 deferred item closed) |
| [D-078](D-078-memory-notes-are-retired-when-their-cluster-merges.md) | Memory notes are retired when their cluster merges or shrinks |
| [D-079](D-079-workflow-versioning-is-a-deploy-checklist-not-a-ci.md) | Workflow versioning is a deploy checklist, not a CI guard |
| [D-080](D-080-chemical-safety-a-deterministic-advisory-structural.md) | Chemical safety: a deterministic, advisory structural screen (never a clearance) |
| [D-081](D-081-config-extensibility-mcp-transport-union-skill.md) | Config-extensibility: MCP transport union, skill manifest + enable-list, config idiom rule (audit doc 10, items 5–7) |
| [D-082](D-082-graph-cache-ttl-da-5-decision-d-1-and-the-helm.md) | Graph-cache TTL (DA-5 / decision D-1) and the Helm render gate (DA-10 / decision D-2) |
| [D-083](D-083-f11-waves-0-3-closing-the-capability-gaps-deployment.md) | F11 waves 0–3: closing the capability gaps (deployment, reachability, chemistry) |
| [D-084](D-084-f11-waves-3-4-operating-the-system-the-knowledge.md) | F11 waves 3–4: operating the system; the knowledge model reasoning about itself |
| [D-085](D-085-f11-completion-the-five-items-blocked-on-a-decision.md) | F11 completion: the five items blocked on a decision or a prerequisite |
| [D-086](D-086-first-reconciliation-with-main-prs-17-20-hazard.md) | First reconciliation with `main` (PRs #17–#20): hazard screen, event sink, tool registry |
| [D-087](D-087-second-reconciliation-with-main-pr-21-the-mcp.md) | Second reconciliation with `main` (PR #21): the MCP transport union |
| [D-088](D-088-third-reconciliation-with-main-pr-23-adr-renumbering.md) | Third reconciliation with `main` (PR #23): ADR renumbering, and the chart's env parity guard |
| [D-089](D-089-no-external-sources-pdf-pptx-docx-xlsx-are-in-scope.md) | No external sources; PDF/PPTX/DOCX/XLSX are in scope |
| [D-090](D-090-reported-issue-sweep-the-azide-the-screener-could.md) | Reported-issue sweep: the azide the screener could not see, two missing session routes, and the note-repo footgun |
| [D-091](D-091-restoring-the-tree-the-replit-restructure-rewound.md) | Restoring the tree the Replit restructure rewound |
| [D-092](D-092-process-analytical-development-capability-research.md) | Process/analytical-development capability research: quick wins, one durable big win, and what was rejected |
| [D-093](D-093-a-raw-exception-in-a-fan-out-child-suspends-as-a.md) | A raw exception in a fan-out child suspends as a task failure, not a workflow failure |
| [D-094](D-094-ci-s-kg-validate-step-needs-a-real-even-empty.md) | CI's `kg-validate` step needs a real (even empty) `knowledge` directory |
| [D-095](D-095-xtb-capability-seams-x1-and-the-properties-the-scf.md) | xTB capability seams (X1) and the properties the SCF already produced (X2) |
| [D-096](D-096-xtb-descriptors-as-bo-featurization-u1.md) | xTB descriptors as BO featurization (U1) |
| [D-097](D-097-the-single-point-runs-on-a-relaxed-geometry-and-the.md) | The single point runs on a relaxed geometry, and the skill catalogue that found it |
| [D-098](D-098-x3-x4-geometries-free-energies-the-reaction.md) | X3/X4: geometries, free energies, the reaction composite, and durable routing |
| [D-099](D-099-durable-capabilities-declare-their-own-queue.md) | Durable capabilities declare their own queue |
| [D-100](D-100-sizing-for-real-substrates-the-workload-is-200-800.md) | Sizing for real substrates: the workload is 200-800 Da |
| [D-101](D-101-x5-x6-x7-the-binaries-and-what-they-change.md) | X5/X6/X7: the binaries, and what they change |
| [D-102](D-102-x9-revisited-preconditioning-the-path-the-binary.md) | X9 revisited: preconditioning the path the binary cannot take |
| [D-103](D-103-x8-the-calculators-as-an-mcp-server-and-the-line.md) | X8: the calculators as an MCP server, and the line identity draws |
| [D-104](D-104-x11-two-molecules-together-and-the-half-of-the-amine.md) | X11: two molecules together, and the half of the amine problem that is refused |
| [D-105](D-105-fourth-reconciliation-with-main-pr-28-the-restored.md) | Fourth reconciliation with `main` (PR #28): the restored tree meets the xTB layer |
| [D-106](D-106-heavy-review-of-the-xtb-layer-five-defects-the-tests.md) | Heavy review of the xTB layer: five defects the tests did not catch |
| [D-107](D-107-fifth-reconciliation-with-main-pr-31-a-unit-boundary.md) | Fifth reconciliation with `main` (PR #31): a unit boundary and a sign, both silent |
| [D-108](D-108-one-conformer-ensemble-one-reaction-composite-the.md) | One conformer ensemble, one reaction composite: the duplicates are removed |
| [D-109](D-109-four-fixes-from-the-live-e2e-pass-and-two-root.md) | Four fixes from the live e2e pass, and two root causes that were not what they looked like |
| [D-110](D-110-the-connector-seam-one-way-to-add-a-tool-a-skill-or.md) | The connector seam: one way to add a tool, a skill, or an agentic workflow |
| [D-111](D-111-stage-c-the-domain-connectors-and-two-defects-the.md) | Stage C: the domain connectors, and two defects the migration surfaced |
| [D-112](D-112-bo-as-the-reference-connector-owned-durable.md) | `bo` as the reference connector-owned durable capability |
| [D-113](D-113-stages-d-and-e-profiles-select-an-agent-templates.md) | Stages D and E: profiles select an agent, templates fix a procedure |
| [D-114](D-114-sixth-reconciliation-with-main-the-xtb-layer-meets.md) | Sixth reconciliation with `main`: the xTB layer meets the connector seam |
| [D-115](D-115-the-two-remaining-stage-c-items-answered-neither.md) | The two remaining Stage C items, answered: neither becomes a bundle |
| [D-116](D-116-seventh-reconciliation-with-main-pr-30-two.md) | Seventh reconciliation with `main` (PR #30): two capabilities the merge silently restored |
| [D-117](D-117-consolidating-the-outstanding-branches-and-deleting.md) | Consolidating the outstanding branches, and deleting what four generations of the design left behind |
| [D-118](D-118-one-connector-seam-for-mcp-temporal-and-long-running.md) | One connector seam for MCP, Temporal and long-running HPC tools |
| [D-119](D-119-production-scale-the-event-loop-the-connection-pool.md) | Production scale: the event loop, the connection pool, and a guard that switched itself off |
| [D-120](D-120-a-data-source-becomes-a-manifest-the-second-config.md) | A data source becomes a manifest: the second config-side union replaced by a folder |
| [D-121](D-121-the-front-door-as-a-multi-process-service-pure-asgi.md) | The front door as a multi-process service: pure-ASGI headers, a durable turn claim, a pool timeout that sheds |
| [D-122](D-122-the-gxp-audit-trail-defaults-to-durable-because.md) | The GxP audit trail defaults to durable, because opting in per call site did not work |
| [D-123](D-123-one-agent-per-concurrent-turn-a-shared-chat-client.md) | One agent per concurrent turn: a shared chat client corrupts streamed tool calls |
| [D-124](D-124-a-calculation-s-by-products-outlive-the-directory-it.md) | The artifact store: a calculation's by-products outlive its tempdir, and the cost policy the cache lacked |
| [D-130](D-130-turn-teardown-runs-in-a-cancelled-task-so-its.md) | Turn teardown runs in a cancelled task, so its cleanup has to be shielded to happen at all |
| [D-131](D-131-the-connector-health-probe-follows-the-address.md) | The connector health probe follows the address override, instead of probing the pod itself |
| [D-132](D-132-the-hessian-is-its-own-calculation-splitting-the.md) | The Hessian is its own calculation: splitting the matrix from the thermochemistry computed over it |
| [D-133](D-133-a-submission-is-a-note-and-what-it-needs-so-a.md) | A submission is a note and what it needs, so a computed result can cite the compound it is about |
| [D-134](D-134-edges-carry-relations-and-their-own-validity-so-the.md) | Edges carry relations and their own validity, so the graph stops being a citation network |
| [D-135](D-135-a-dataset-may-be-vendored-into-the-image-at-build.md) | A dataset may be vendored into the image at build time — the one amendment to D-089's scope |
| [D-136](D-136-the-shipped-defaults-were-never-executed-three.md) | The shipped defaults were never executed: three configurations that fail on first contact |
| [D-137](D-137-the-plan-the-model-could-approve-for-itself-a-pre.md) | The plan the model could approve for itself: a pre-execution gate that is not a tool |
| [D-138](D-138-fifty-questions-asked-live-the-job-surface-was-dead.md) | Fifty questions, asked live: the job surface was dead, the trace was blind, and a failed tool was silent |
| [D-139](D-139-three-silent-failures-a-degraded-turn-a-pooled.md) | Three silent failures: a degraded turn, a pooled calibration, and two counters wired to nothing |
| [D-140](D-140-a-template-s-job-step-resolved-off-the-workflow.md) | A template's job step: resolved off the workflow thread, and finally able to fail |
| [D-141](D-141-two-facts-that-stopped-at-a-process-boundary-a.md) | Two facts that stopped at a process boundary: a session's profile, and the turn's correlation id |
| [D-142](D-142-a-production-value-has-to-be-executed-not-type.md) | A production value has to be executed, not type-checked — and two guards that were off in the one deployment that needed them |
| [D-143](D-143-nobody-was-collecting-the-metrics-and-the-durable.md) | Nobody was collecting the metrics, and the durable history is never compacted — one fixed, one where the obvious fix corrupts data |
| [D-144](D-144-token-accounting-was-priced-blind-one-total-where.md) | Token accounting was priced-blind: one total where the bill has four line items |
| [D-145](D-145-a-conversation-row-cannot-be-disposed-of-without-the.md) | A conversation row cannot be disposed of without the rows it is paired with |
| [D-146](D-146-the-service-is-the-repository-removing-the-services.md) | The service is the repository: removing the `services/` tier the Replit monorepo left behind |
| [D-147](D-147-one-file-per-adr-and-a-docs-tree-with-an-archive.md) | One file per ADR, and a `docs/` tree with a living half and an archive |
| [D-148](D-148-the-packages-regrouped-under-src-chemclaw-by-layer.md) | The packages regrouped under `src/chemclaw/` by the four architecture layers |
| [D-149](D-149-what-two-finished-migrations-left-behind-and-a-guard.md) | What two finished migrations left behind, and the guard for the kind that rots silently |
| [D-150](D-150-a-connector-jobs-task-queue-is-derived-not-declared.md) | A connector job's task queue is derived, not declared |
| [D-151](D-151-the-durable-history-compacts-itself-because-maf-s.md) | The durable history compacts itself, because MAF's after-run compaction cannot reach it |
| [D-152](D-152-metrics-carry-labels-caching-is-measured-not-built.md) | Metrics carry labels, caching is measured rather than built, and the CLI meets the harness |
| [D-153](D-153-the-mid-turn-wait-asks-the-jobs-not-the-mailbox.md) | The mid-turn wait asks the jobs, not the mailbox |
| [D-154](D-154-a-register-that-had-become-a-log-and-the-one.md) | A register that had become a log, and the one trigger it was hiding |
| [D-155](D-155-what-the-dark-half-of-the-system-does-the-first-time.md) | What the dark half of the system does the first time it runs |
| [D-156](D-156-the-last-false-duplicate-and-a-map-that-is-enforced.md) | The last false duplicate, the corpora in one place, and a map that is enforced |
| [D-157](D-157-a-durable-record-of-every-connector-job-what-ran.md) | A durable record of every connector job: what ran, with what data, and why |
| [D-158](D-158-the-expensive-calculation-is-the-one-that-was-not.md) | The expensive calculation is the one that was not cached |
| [D-159](D-159-the-turn-stream-reports-a-tool-s-lifecycle-not.md) | The turn stream reports a tool's lifecycle, not just that a call happened |
| [D-160](D-160-retrieval-carries-provenance-so-a-claim-can-be.md) | Retrieval carries provenance, so a claim can be qualified by who authored its evidence |
| [D-161](D-161-the-human-gate-moves-from-every-observation-to.md) | The human gate moves from every observation to the few worth promoting |
| [D-162](D-162-a-series-of-experiments-is-a-sequence-not-a-set.md) | A series of experiments is a sequence, not a set |
| [D-163](D-163-a-store-you-can-only-address-is-not-a-store-you.md) | A store you can only address is not a store you can ask |
| [D-164](D-164-the-prose-gate-learns-note-types-and-the-two-dead.md) | The prose gate learns note types, and the two dead ones it finds |
| [D-165](D-165-a-cited-artifact-the-agent-can-open-and-the-ones.md) | A cited artifact the agent can open, and the ones it should not try to read |
| [D-166](D-166-the-queue-is-reported-on-the-stream-not-as-a.md) | The queue is reported on the stream, not as a refusal |
| [D-167](D-167-an-approval-authorizes-a-request-not-a-session.md) | An approval authorizes a request, not a session |
| [D-168](D-168-a-template-step-runs-as-its-requester.md) | A template step runs as its requester, and four steps that had never run |
| [D-169](D-169-trust-is-a-distribution-not-a-number-the-residual.md) | Trust is a distribution, not a number: the residual listing, and the property table behind it |
| [D-170](D-170-a-similarity-hit-you-cannot-qualify-is-a.md) | A similarity hit you cannot qualify is a similarity hit you cannot use |
| [D-2026-07-31-a-campaign-is-an-entity-not-a-turn](D-2026-07-31-a-campaign-is-an-entity-not-a-turn.md) | A campaign is an entity, not a turn |
| [D-2026-07-31-a-proposal-is-a-record-not-a-branch](D-2026-07-31-a-proposal-is-a-record-not-a-branch.md) | A proposal is a record, not a branch |
| [D-2026-07-31-adr-ids-that-cannot-collide](D-2026-07-31-adr-ids-that-cannot-collide.md) | ADR ids that cannot collide |
| [D-2026-07-31-an-eln-entry-is-versioned-not-immutable](D-2026-07-31-an-eln-entry-is-versioned-not-immutable.md) | An ELN entry is versioned, not immutable |
| [D-2026-07-31-one-gate-over-one-side-effecting-set](D-2026-07-31-one-gate-over-one-side-effecting-set.md) | One gate over one side-effecting set |
| [D-2026-07-31-the-audit-chain-is-versioned](D-2026-07-31-the-audit-chain-is-versioned.md) | The audit chain is versioned, so widening the record does not invalidate it |
| [D-2026-07-31-the-deployment-envelope](D-2026-07-31-the-deployment-envelope.md) | The deployment envelope: a sidecar that emptied the tree, and three assertions the chart never made |
| [D-2026-07-31-two-spellings-of-one-molecule](D-2026-07-31-two-spellings-of-one-molecule.md) | Two spellings of one molecule, and two questions about them |
| [D-2026-08-01-a-cap-that-starves-a-source](D-2026-08-01-a-cap-that-starves-a-source.md) | A cap that starves a source |
| [D-2026-08-01-a-cheap-request-is-still-a-request](D-2026-08-01-a-cheap-request-is-still-a-request.md) | A cheap request is still a request, and a checked upload is still an ingested one |
| [D-2026-08-01-a-declaration-that-authorizes-nothing](D-2026-08-01-a-declaration-that-authorizes-nothing.md) | A declaration that authorizes nothing |
| [D-2026-08-01-a-drain-is-not-a-kill-with-extra-steps](D-2026-08-01-a-drain-is-not-a-kill-with-extra-steps.md) | A drain is not a kill with extra steps |
| [D-2026-08-01-a-gate-that-leaks-on-the-failure-path](D-2026-08-01-a-gate-that-leaks-on-the-failure-path.md) | A gate that leaks on the failure path |
| [D-2026-08-01-a-key-names-what-ran](D-2026-08-01-a-key-names-what-ran.md) | A calculation key names every program that produced it |
| [D-2026-08-01-a-key-that-cannot-see-our-own-fix](D-2026-08-01-a-key-that-cannot-see-our-own-fix.md) | A key that cannot see our own fix |
| [D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not](D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not.md) | A log line that joins, and a secret that does not |
| [D-2026-08-01-a-migration-waits-in-front-of-live-traffic](D-2026-08-01-a-migration-waits-in-front-of-live-traffic.md) | A migration that waits, waits in front of live traffic |
| [D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check](D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check.md) | A path in prose is a claim, and a gate can check it |
| [D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down](D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down.md) | A per-process cap, multiplied by a number nobody wrote down |
| [D-2026-08-01-a-reagent-is-not-its-largest-fragment](D-2026-08-01-a-reagent-is-not-its-largest-fragment.md) | A reagent is not its largest fragment |
| [D-2026-08-01-a-restore-is-a-truncation-nobody-can-see](D-2026-08-01-a-restore-is-a-truncation-nobody-can-see.md) | A restore is a truncation nobody can see |
| [D-2026-08-01-a-rule-that-counts-cannot-be-a-chain](D-2026-08-01-a-rule-that-counts-cannot-be-a-chain.md) | A rule that counts cannot be a chain |
| [D-2026-08-01-a-running-job-has-no-owner](D-2026-08-01-a-running-job-has-no-owner.md) | A running job has no owner, so cancelling one is an operator action |
| [D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment](D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment.md) | A scripted transcript gates the harness, not the judgment |
| [D-2026-08-01-a-tag-is-a-pointer-not-a-build](D-2026-08-01-a-tag-is-a-pointer-not-a-build.md) | A tag is a pointer, not a build |
| [D-2026-08-01-a-turn-you-can-follow-across-a-process](D-2026-08-01-a-turn-you-can-follow-across-a-process.md) | A turn you can follow across a process |
| [D-2026-08-01-every-process-carries-its-own-witness](D-2026-08-01-every-process-carries-its-own-witness.md) | Every process carries its own witness, and the sentence that stopped two of them |
| [D-2026-08-01-one-equilibrium-or-no-number](D-2026-08-01-one-equilibrium-or-no-number.md) | One equilibrium, or no number |
| [D-2026-08-01-spend-is-a-ledger-not-a-label](D-2026-08-01-spend-is-a-ledger-not-a-label.md) | Spend is a ledger, not a label |
| [D-2026-08-01-symmetry-is-an-input-not-a-default](D-2026-08-01-symmetry-is-an-input-not-a-default.md) | Symmetry is an input, not a default |
| [D-2026-08-01-the-agent-slot-that-changed-no-bits](D-2026-08-01-the-agent-slot-that-changed-no-bits.md) | The agent slot that changed no bits |
| [D-2026-08-01-the-cap-reports-itself](D-2026-08-01-the-cap-reports-itself.md) | The cap reports itself |
| [D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose](D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose.md) | The count lives in the test, not in the prose |
| [D-2026-08-01-trust-travels-on-the-value-line](D-2026-08-01-trust-travels-on-the-value-line.md) | Trust travels on the value line |
| [D-2026-08-01-unknown-is-not-fine](D-2026-08-01-unknown-is-not-fine.md) | "Unknown" is not "fine": one shape for how much to trust a number |
| [D-2026-08-02-a-limit-is-data-a-classification-is-a-model](D-2026-08-02-a-limit-is-data-a-classification-is-a-model.md) | A limit is data; a classification is a model |
| [D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet](D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet.md) | A probe is a question you have not asked yet |
| [D-2026-08-02-a-solvent-charge-is-a-volume](D-2026-08-02-a-solvent-charge-is-a-volume.md) | A solvent charge is a volume |
| [D-2026-08-02-grounding-is-what-this-turn-saw](D-2026-08-02-grounding-is-what-this-turn-saw.md) | Grounding is what this turn saw |
| [D-2026-08-02-shipped-is-not-reachable](D-2026-08-02-shipped-is-not-reachable.md) | Shipped is not reachable |
| [D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate](D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate.md) | The fraction lives where BoFire will fractionate |
| [D-2026-08-02-the-seam-does-not-move](D-2026-08-02-the-seam-does-not-move.md) | `core/config` becomes a package, the import seam stays |
| [D-2026-08-02-work-repeated-every-time-for-no-reason](D-2026-08-02-work-repeated-every-time-for-no-reason.md) | Two costs proportional to the whole corpus, paid on every run |
| [D-2026-08-03-a-metric-must-declare-what-it-can-see](D-2026-08-03-a-metric-must-declare-what-it-can-see.md) | A metric must declare what it can see |
| [D-2026-08-03-the-refactor-closes-what-it-measured](D-2026-08-03-the-refactor-closes-what-it-measured.md) | Closing the grand refactor on re-measured numbers |
| [D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed](D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed.md) | A failure that says nothing is read as "proceed" |
| [D-2026-08-04-a-lane-that-only-runs-where-docker-runs](D-2026-08-04-a-lane-that-only-runs-where-docker-runs.md) | A lane that only runs where Docker runs is a lane that does not run |
| [D-2026-08-04-a-limit-across-parameters-is-not-a-bound](D-2026-08-04-a-limit-across-parameters-is-not-a-bound.md) | A limit across parameters is not a bound |
| [D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with](D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with.md) | A plateau needs the noise you measured it with |
| [D-2026-08-04-a-screen-may-hold-a-continuous-factor-at-its-bounds](D-2026-08-04-a-screen-may-hold-a-continuous-factor-at-its-bounds.md) | A screen may hold a continuous factor at its bounds |
| [D-2026-08-04-a-trade-off-has-no-single-best-point](D-2026-08-04-a-trade-off-has-no-single-best-point.md) | A trade-off has no single best point |
| [D-2026-08-04-the-model-can-be-asked-not-only-obeyed](D-2026-08-04-the-model-can-be-asked-not-only-obeyed.md) | The model can be asked, not only obeyed |
| [D-2026-08-04-the-schema-is-a-file](D-2026-08-04-the-schema-is-a-file.md) | A warehouse ELN's schema is a binding document, not an adapter |
| [D-2026-08-04-the-schema-only-goes-forward](D-2026-08-04-the-schema-only-goes-forward.md) | The schema only goes forward, and a test says so |
| [D-2026-08-04-what-bofire-does-when-you-actually-run-it](D-2026-08-04-what-bofire-does-when-you-actually-run-it.md) | What BoFire does when you actually run it, and the roadmap that survived it |
| [D-2026-08-05-a-ceiling-that-does-not-hold](D-2026-08-05-a-ceiling-that-does-not-hold.md) | A ceiling that does not hold, and four writes that could tear |
| [D-2026-08-05-a-declaration-outliving-what-it-describes](D-2026-08-05-a-declaration-outliving-what-it-describes.md) | A declaration outliving what it describes |
| [D-2026-08-05-a-gain-is-measured-from-the-last-gain](D-2026-08-05-a-gain-is-measured-from-the-last-gain.md) | A gain is measured from the last gain, not from the last run |
| [D-2026-08-05-a-score-reported-more-precisely-than-it-repeats](D-2026-08-05-a-score-reported-more-precisely-than-it-repeats.md) | A score reported more precisely than it repeats |
| [D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches](D-2026-08-05-a-skill-that-outlives-the-tools-it-teaches.md) | A skill that outlives the tools it teaches |
| [D-2026-08-05-a-sweep-that-commits-once](D-2026-08-05-a-sweep-that-commits-once.md) | A sweep that commits once can lose everything it did |
| [D-2026-08-05-a-trend-needs-a-tail](D-2026-08-05-a-trend-needs-a-tail.md) | A trend needs a tail, not just a slope |
| [D-2026-08-05-a-worker-may-not-outrun-its-pool](D-2026-08-05-a-worker-may-not-outrun-its-pool.md) | A worker may not admit more activities than its pool can serve |
| [D-2026-08-05-append-only-by-grant-not-by-contract](D-2026-08-05-append-only-by-grant-not-by-contract.md) | Append-only by grant, not by contract |
| [D-2026-08-05-one-rule-in-three-places-is-three-rules](D-2026-08-05-one-rule-in-three-places-is-three-rules.md) | One rule written in three places is three rules |
| [D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without](D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without.md) | Readiness answers for the store it cannot serve without |
| [D-2026-08-05-the-connection-budget-is-a-fleet-number](D-2026-08-05-the-connection-budget-is-a-fleet-number.md) | The connection budget is a fleet number, and the pool's witness belongs to the pool |
| [D-2026-08-05-three-searches-that-disagreed-about-one-note](D-2026-08-05-three-searches-that-disagreed-about-one-note.md) | Three searches that disagreed about one note, and a gate that borrowed the tree it guards |
| [D-2026-08-06-a-flag-is-a-signal-not-an-inventory](D-2026-08-06-a-flag-is-a-signal-not-an-inventory.md) | A flag is a signal, not an inventory |
| [D-2026-08-06-a-gate-that-names-nothing](D-2026-08-06-a-gate-that-names-nothing.md) | A gate that names nothing |
| [D-2026-08-06-a-pair-rule-is-a-cross-product](D-2026-08-06-a-pair-rule-is-a-cross-product.md) | A pair rule is a cross-product, and the list is the caller's |
| [D-2026-08-06-a-redactor-that-only-reads-the-message](D-2026-08-06-a-redactor-that-only-reads-the-message.md) | A redactor that only reads the message |
| [D-2026-08-06-a-share-is-mounted-not-called](D-2026-08-06-a-share-is-mounted-not-called.md) | A classical file share becomes a corpus, and its AD group becomes an entitlement |
| [D-2026-08-06-a-swallowed-write-reported-as-a-store](D-2026-08-06-a-swallowed-write-reported-as-a-store.md) | A swallowed write, reported as a store |
| [D-2026-08-06-a-tool-cannot-say-it-has-nothing-twice](D-2026-08-06-a-tool-cannot-say-it-has-nothing-twice.md) | A tool cannot say it has nothing twice |
| [D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it](D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it.md) | The embedding configuration is part of a vector's identity |
| [D-2026-08-06-an-envelope-that-only-survives-its-own-process](D-2026-08-06-an-envelope-that-only-survives-its-own-process.md) | An envelope that only survives its own process |
| [D-2026-08-06-the-caller-chooses-the-kid-not-the-workload](D-2026-08-06-the-caller-chooses-the-kid-not-the-workload.md) | The caller chooses the `kid`, not how much work we do about it |
| [D-2026-08-06-the-memo-already-carried-the-actor](D-2026-08-06-the-memo-already-carried-the-actor.md) | The memo already carried the actor |
| [D-2026-08-06-the-method-decides-which-solvents-exist](D-2026-08-06-the-method-decides-which-solvents-exist.md) | The method decides which solvents exist, and it can be asked |
| [D-2026-08-07-a-manifest-must-say-who-may-read-it](D-2026-08-07-a-manifest-must-say-who-may-read-it.md) | The mount is a boundary, and omission is not a decision |
| [D-2026-08-07-one-bad-file-must-not-stop-the-corpus](D-2026-08-07-one-bad-file-must-not-stop-the-corpus.md) | The guard belongs at the boundary, not on the constructor |
| [D-2026-08-07-the-mark-means-observed-not-processed](D-2026-08-07-the-mark-means-observed-not-processed.md) | The sweep reads the drain's own evidence |
| [D-2026-08-08-a-borrowed-connection-is-bounded-by-default](D-2026-08-08-a-borrowed-connection-is-bounded-by-default.md) | The safe bound is the default, and the escape hatch is a different function |
| [D-2026-08-08-a-bundle-may-extend-a-closed-vocabulary](D-2026-08-08-a-bundle-may-extend-a-closed-vocabulary.md) | Note types and relations are declared, not written into core |
| [D-2026-08-08-a-category-has-no-outside](D-2026-08-08-a-category-has-no-outside.md) | The two BO tool-surface defects an audit found, and the eight it refuted |
| [D-2026-08-08-a-degraded-check-must-not-clear-the-gate](D-2026-08-08-a-degraded-check-must-not-clear-the-gate.md) | The substitute was more generous |
| [D-2026-08-08-a-derived-index-must-record-what-derived-it](D-2026-08-08-a-derived-index-must-record-what-derived-it.md) | A derived index must record what derived it |
| [D-2026-08-08-a-partial-answer-must-say-so](D-2026-08-08-a-partial-answer-must-say-so.md) | Seven science defects that render as clean results |
| [D-2026-08-08-a-prefix-the-documents-never-carried](D-2026-08-08-a-prefix-the-documents-never-carried.md) | The string a gate matches on belongs to one definition, and the prose that teaches it is checked |
| [D-2026-08-08-a-private-import-of-a-type-alias-is-not-a-dependency](D-2026-08-08-a-private-import-of-a-type-alias-is-not-a-dependency.md) | A private import of a type alias is not a dependency |
| [D-2026-08-08-a-rollback-that-is-not-a-schema-step](D-2026-08-08-a-rollback-that-is-not-a-schema-step.md) | A rollback that is not a schema step |
| [D-2026-08-08-a-rule-with-no-test-is-a-claim](D-2026-08-08-a-rule-with-no-test-is-a-claim.md) | The enforcement layer for rules this repository already states |
| [D-2026-08-08-a-served-tool-is-a-reachable-tool](D-2026-08-08-a-served-tool-is-a-reachable-tool.md) | The allow-list guarded the agent, not the port |
| [D-2026-08-08-a-slot-lives-as-long-as-its-response](D-2026-08-08-a-slot-lives-as-long-as-its-response.md) | A slot lives as long as its response, and a check that runs before the queue checks nothing |
| [D-2026-08-08-a-source-is-named-by-its-folder-not-by-its-half](D-2026-08-08-a-source-is-named-by-its-folder-not-by-its-half.md) | The registry tells a retrieve half which source it is |
| [D-2026-08-08-a-survivor-is-a-hypothesis](D-2026-08-08-a-survivor-is-a-hypothesis.md) | A survivor is a hypothesis, not a finding |
| [D-2026-08-08-a-test-that-survives-the-mutation-it-names](D-2026-08-08-a-test-that-survives-the-mutation-it-names.md) | A test that survives the mutation it names |
| [D-2026-08-08-a-vector-store-is-not-a-catalogue](D-2026-08-08-a-vector-store-is-not-a-catalogue.md) | Only the dense half is pluggable, and the rest stays in Postgres |
| [D-2026-08-08-an-outage-is-not-a-missing-job](D-2026-08-08-an-outage-is-not-a-missing-job.md) | Six durable failures that reported the wrong thing |
| [D-2026-08-08-identity-must-travel-with-the-work](D-2026-08-08-identity-must-travel-with-the-work.md) | A role name is not an entitlement |
| [D-2026-08-08-redaction-must-outlive-the-formatter](D-2026-08-08-redaction-must-outlive-the-formatter.md) | The leak lived in the path no test took |
| [D-2026-08-08-the-conversation-is-erasable-the-record-is-not](D-2026-08-08-the-conversation-is-erasable-the-record-is-not.md) | What offboarding removes, and what it refuses to |
| [D-2026-08-08-the-inventory-that-vouched-for-itself](D-2026-08-08-the-inventory-that-vouched-for-itself.md) | Seven claims, re-measured, and the two that became tests |
| [D-2026-08-09-a-connector-we-do-not-run](D-2026-08-09-a-connector-we-do-not-run.md) | Hosting is a deployment fact, and the URL is the whole knob |
| [D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one](D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one.md) | A derivable ref is not a fetchable one, so the transcript checks before it advertises |
| [D-2026-08-09-a-hand-written-list-of-columns-drifts](D-2026-08-09-a-hand-written-list-of-columns-drifts.md) | Seven review findings against the offboarding and seam work |
| [D-2026-08-09-a-preview-is-not-a-result](D-2026-08-09-a-preview-is-not-a-result.md) | A preview is not a result, so give the result somewhere to live |
| [D-2026-08-09-a-scope-that-matches-no-point](D-2026-08-09-a-scope-that-matches-no-point.md) | The group moved to the cutting and the scope stayed at the document |
| [D-2026-08-09-a-twin-rule-is-one-string](D-2026-08-09-a-twin-rule-is-one-string.md) | A twin rule is one string, and a guard must be measured |
| [D-2026-08-09-a-valid-prefix-is-not-a-molecule](D-2026-08-09-a-valid-prefix-is-not-a-molecule.md) | A valid prefix is not a molecule, so a hazard screen refuses it |
| [D-2026-08-10-a-list-of-ids-is-not-a-conversation-list](D-2026-08-10-a-list-of-ids-is-not-a-conversation-list.md) | A list of ids is not a conversation list, so the service names and orders its own sessions |
| [D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor](D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md) | A specialist inherits the caller's authority, narrowed |
| [D-2026-08-10-basestore-is-not-where-this-systems-memory-lives](D-2026-08-10-basestore-is-not-where-this-systems-memory-lives.md) | BaseStore is not adopted; the memory package emits notes, not rows |
| [D-2026-08-10-langgraph-rebuild-of-the-conversation-layer](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md) | Layer 1 is rebuilt on LangGraph, and turn state stops being hand-built |
| [D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs](D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs.md) | A handoff is observable where the specialist runs, not where it was dispatched |
| [D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment](D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment.md) | LLM spans through OpenInference, with content suppressed by default |
| [D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has](D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has.md) | The deep-agents audit, and the context policy the framework removal took with it |
| [D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate](D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate.md) | The announcer must wrap everything that refuses, not sit beneath it |
| [D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape](D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape.md) | LangSmith is declined; the gaps it would fill are named and split |
| [D-2026-08-11-the-specialists-name-is-not-in-the-namespace](D-2026-08-11-the-specialists-name-is-not-in-the-namespace.md) | Attribute a specialist's events from the handoff, because the graph path never held its name |
| [D-2026-08-11-what-the-removal-found](D-2026-08-11-what-the-removal-found.md) | Deleting the framework is what exposed the readers that only knew one shape |
| [D-2026-08-11-what-the-review-found-in-the-compaction-change](D-2026-08-11-what-the-review-found-in-the-compaction-change.md) | A middleware that narrowed its engine, a privacy flag that re-armed itself, and a placeholder arguing with a guard |
| [D-2026-08-12-a-held-permit-is-the-price-of-a-mid-turn-resume](D-2026-08-12-a-held-permit-is-the-price-of-a-mid-turn-resume.md) | Mid-turn resume stays off, because the push-back mailbox already answers its question for free |
| [D-2026-08-12-a-review-the-migration-did-not-get](D-2026-08-12-a-review-the-migration-did-not-get.md) | What 181 reviewers found in a migration that shipped green |
| [D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate](D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate.md) | Delegation is unmeasurable when the supervisor is a superset of every specialist, so routing is scored on the surface a turn used |
| [D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only](D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only.md) | A template is the pre-approved plan, so its agent step is ungated and read-only by default |
| [D-2026-08-12-the-cache-floor-is-per-model-and-two-profiles-are-under-it](D-2026-08-12-the-cache-floor-is-per-model-and-two-profiles-are-under-it.md) | The minimum cacheable prefix measured: 4,096 on haiku, 1,024 on sonnet, and two shipped profiles below it |
| [D-2026-08-12-the-cap-was-right-and-what-it-was-holding-back](D-2026-08-12-the-cap-was-right-and-what-it-was-holding-back.md) | Lifting `deepagents<0.7`, and the write verb it was holding back |
| [D-2026-08-12-the-experiment-surface-is-the-record-somebody-can-open](D-2026-08-12-the-experiment-surface-is-the-record-somebody-can-open.md) | AG-13's backend: Phoenix as an eval-lane deployment, fed from transcripts that already exist |
| [D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it](D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it.md) | Prompt caching in the F0 seam, and the cache-write key the ledger was not reading |
| [D-2026-08-13-a-checkpoint-says-which-schema-wrote-it](D-2026-08-13-a-checkpoint-says-which-schema-wrote-it.md) | Every checkpoint carries the state schema it was written under, and a foreign one is refused by name |
| [D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks](D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks.md) | The five specialists become tool surfaces, delegation goes back to upstream's reason, and a bare `SubAgent` dict is forbidden |
| [D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim](D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim.md) | A writer that cannot authenticate its caller records `unverified:<id>`, and erasure knows both spellings of one person |
| [D-2026-08-13-analyze-first-and-the-recall-knobs-second](D-2026-08-13-analyze-first-and-the-recall-knobs-second.md) | Two pgvector recall knobs exist, default to emitting nothing, and are documented as the residual after `ANALYZE` |
| [D-2026-08-13-both-lexical-backends-state-one-boolean-rule](D-2026-08-13-both-lexical-backends-state-one-boolean-rule.md) | The note index matches any term and ranks the complete matches first, in both backends |
| [D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared](D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared.md) | An answer is attacked by agents briefed for it, and a team is attacked unconditionally |
| [D-2026-08-13-the-guard-must-not-refuse-a-dependency-bump](D-2026-08-13-the-guard-must-not-refuse-a-dependency-bump.md) | The checkpoint stamp covers the channels this repository declares, and refuses only the direction that was measured to fail |

| [D-2026-08-14-the-coupling-is-the-cost-not-the-line-count](D-2026-08-14-the-coupling-is-the-cost-not-the-line-count.md) | What upstream already does, what it still does not, and the shapes this repository reads that it was never promised |
| [D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks](D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks.md) | GxP framing and the audit hash chain are removed; the trail, the gates and the INSERT-only grant stay |
| [D-2026-08-14-two-http-stacks-is-the-price-of-the-openai-major](D-2026-08-14-two-http-stacks-is-the-price-of-the-openai-major.md) | The dependency bump: openai 3 and starlette 1.6 taken with httpx2 beside httpx, and the `mcp<2` cap kept |

| [D-2026-08-15-a-bundle-we-declare-is-not-a-bundle-we-run](D-2026-08-15-a-bundle-we-declare-is-not-a-bundle-we-run.md) | `chem`'s capability moves to Chemclaw3-mcp and its manifest stays, because four validators resolve tool names through it |
| [D-2026-08-15-a-capability-that-ships-off-is-not-a-capability](D-2026-08-15-a-capability-that-ships-off-is-not-a-capability.md) | The specialist team, the challenge panel and the routing measurement are deleted rather than left disabled |
| [D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit](D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit.md) | GitHub Issues own who is working on a backlog item; `BACKLOG.md` stays the prioritized list |
| [D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently](D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently.md) | Layer 1 compiles on `create_deep_agent`; the `task` tool it forces is governed, and the summarizer it brings is switched off |
| [D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work](D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work.md) | The agent gets a scratchpad, and durable memories behind an actor-keyed namespace |
| [D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped](D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped.md) | The runaway cap returns to a first-party `before_model` hook, and the four regressions delegating it produced |
| [D-2026-08-15-capability-moves-judgment-and-declaration-stay](D-2026-08-15-capability-moves-judgment-and-declaration-stay.md) | `chem` is declared here and served by `Chemclaw3-mcp`: capability moves, judgment and declaration stay |
| [D-2026-08-15-safety-is-a-tool-not-a-gate](D-2026-08-15-safety-is-a-tool-not-a-gate.md) | The hazard screen becomes an ordinary MCP capability; the kg-validate gate, the 1.0 eval floor, `safety-validate` and all four settings are deleted |
| [D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question](D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question.md) | `HumanInTheLoopMiddleware` is declined for plan approval: `when` cannot be async, a new message discards a pending interrupt, a mismatched resume bypasses the gate, and retention deletes it |
| [D-2026-08-16-a-cache-that-lets-every-caller-miss-together](D-2026-08-16-a-cache-that-lets-every-caller-miss-together.md) | Layer 4 review: concurrent cold reads each parsed the corpus, a duplicate note id collapsed silently, dangling targets ranked as top hubs, and typed edges reached no reader |
| [D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs](D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs.md) | The job path declares `failure_exception_types`, notifies its session on every bad ending, and stops retrying whole children |
| [D-2026-08-16-a-key-the-caller-cannot-see-is-a-key-the-caller-can-poison](D-2026-08-16-a-key-the-caller-cannot-see-is-a-key-the-caller-can-poison.md) | Carrying out the `calc` split: two tools sharing one key with different payloads, a Fukui key that does not name the mode, an inverted `multiplicity=None`, and the version read off the payload |
| [D-2026-08-16-a-result-too-big-for-its-row-is-an-artifact](D-2026-08-16-a-result-too-big-for-its-row-is-an-artifact.md) | The Hessian goes back to the content-addressed artifact store: a payload too large for its row is offloaded by a `ResultStore` wrapper, restoring D-124's eviction path |
| [D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name](D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name.md) | The six tables LangGraph creates for itself are granted explicitly and guarded per table: `REVOKE ALL` strips even the owning role, so the second deploy took every turn down at its first checkpoint write |
| [D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer](D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer.md) | `RubricMiddleware` is declined: it cannot reuse `score_answer`, and every non-satisfied termination returns the ungraded answer |
| [D-2026-08-16-an-announcement-is-not-a-failure](D-2026-08-16-an-announcement-is-not-a-failure.md) | Eight backlog rows: `failed_loudly` stops counting a pre-turn capability announcement, a failed template wakes its session, a campaign's direction is checked against its registered objective, and two CI checks start running |
| [D-2026-08-16-arithmetic-about-a-loop-is-derived-not-configured](D-2026-08-16-arithmetic-about-a-loop-is-derived-not-configured.md) | Two HPC timing relations become derived properties rather than a validator that would refuse the shipped chart; the launcher credential becomes required |
| [D-2026-08-16-one-tables-failure-should-not-starve-the-rest](D-2026-08-16-one-tables-failure-should-not-starve-the-rest.md) | A database-setup review's six fixes — retention's per-table isolation, `memory_store()`'s cold-start race, unclosed pools on shutdown, a pool health check, two DSN-redaction bypasses, an overstated docstring — and three findings left as `BACKLOG.md` rows |
| [D-2026-08-16-the-handshake-already-says-which-build-answered](D-2026-08-16-the-handshake-already-says-which-build-answered.md) | The MCP `initialize()` handshake carries each server's build revision into `audit_events.tool_revision`, beside the orchestrator's own |
| [D-2026-08-16-the-physics-leaves-the-cache-stays](D-2026-08-16-the-physics-leaves-the-cache-stays.md) | `calc` splits by composability, not speed: primitives move, composites are decomposed, the cache and the durable jobs stay |
| [D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two](D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two.md) | The four-repo lane starts all five Chemclaw3-mcp servers and exports all five tokens; `/readyz` is a connector probe, not a dependency probe |
| [D-2026-08-17-a-queue-row-is-a-claim-and-claims-go-stale](D-2026-08-17-a-queue-row-is-a-claim-and-claims-go-stale.md) | Every anchor in both registers checked against HEAD, and the audit's own numbers corrected the same day when they went stale too |
| [D-2026-08-17-a-workflow-type-is-a-launch-contract-not-a-durability-leak](D-2026-08-17-a-workflow-type-is-a-launch-contract-not-a-durability-leak.md) | A launcher may import the workflow type it launches when that closure is already in its process; a bundle's workflow goes by name, and the agent layer now has the guard core's worker already had |
| [D-2026-08-20-a-networkpolicy-selects-peers-not-paths](D-2026-08-20-a-networkpolicy-selects-peers-not-paths.md) | `bo`, `calc`, `molfp` and `rxnfp` authenticate their own `/mcp`; turning it on exposed a probe allowlist that never matched under a mount |
| [D-2026-08-20-a-tenant-is-a-jwks-document-and-an-issuer-string](D-2026-08-20-a-tenant-is-a-jwks-document-and-an-issuer-string.md) | A stand-in Entra tenant makes the enforced identity path runnable and mutation-proved, in CI and in the live lane; the browser leg is named as the one hop still unproven |
| [D-2026-08-20-a-ui-that-cannot-authenticate-is-not-a-fallback](D-2026-08-20-a-ui-that-cannot-authenticate-is-not-a-fallback.md) | Three identity-audit findings: the bundled UI is dev-only, three readerless Entra settings and their ConfigMap value are deleted, and a group-claim overage becomes an alertable counter |
| [D-2026-08-21-a-geometry-is-an-address-not-a-payload](D-2026-08-21-a-geometry-is-an-address-not-a-payload.md) | A computed geometry reaches the model as a `structure_id` the next calculation accepts, not as coordinates it cannot use; the campaign key stops forking on case, and a template can chain a field |
| [D-2026-08-25-a-cache-is-not-a-record](D-2026-08-25-a-cache-is-not-a-record.md) | Every computed value is published as a typed, queryable scientific record to an external database: a third manifest seam, a schema this repo ships and a site creates, and a durable outbox drained by Temporal |
| [D-2026-08-25-a-chunk-cap-is-not-a-context-budget](D-2026-08-25-a-chunk-cap-is-not-a-context-budget.md) | `gather_evidence` gains a character bound beside its chunk count (240- vs 1800-char chunks, 7.5x apart and unnormalised) and a return type that can say it was cut or that a source was down |
| [D-2026-08-25-a-corpus-is-evidence-not-an-eln](D-2026-08-25-a-corpus-is-evidence-not-an-eln.md) | Pistachio attaches as a retrieve-only source with a `corpus:` binding and no PR-gate: five paths assume an ingest source is one site's ELN, and each breaks on millions of patent reactions |
| [D-2026-08-25-a-label-is-derived-not-recorded](D-2026-08-25-a-label-is-derived-not-recorded.md) | A reaction's roles, name and features are a derived, versioned index beside the fingerprint — not a wider `Role`; staleness is a query and `provides` is never a skip |
| [D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one](D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one.md) | Databricks attaches as a `VectorStore` and a `Warehouse` driver; the score is converted to a cosine, the similarity dialect moves off `sql.py`, notes join the seam with the prune they never had, and Pistachio's third-party corpus is argued against D-089 |
| [D-2026-08-25-a-sandbox-is-a-server-not-a-verb](D-2026-08-25-a-sandbox-is-a-server-not-a-verb.md) | The agent gets numpy/pandas/scipy/RDKit through one stateless MCP server in the fleet that already cannot reach the network; deepagents' `execute` verb stays withheld and no core file changes |
| [D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool](D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool.md) | Why an LLM condensation behind a tool is not the summarization this repo declines, asserted by a compiled-graph audit test rather than argued; many whole protocols become one comparison, measured at 9.1x and never split |
| [D-2026-08-25-an-eln-transcription-is-data-not-a-claim](D-2026-08-25-an-eln-transcription-is-data-not-a-claim.md) | A deterministic ELN transcription is a Postgres row rather than a PR-gated note — 202 ms of serialized git and a human merge per entry bought nothing a reviewer could decide, and the corpus scan behind it wedged the sync at ~700k; no Schedule opens a pull request any more |
| [D-2026-08-25-the-labeller-leaves-the-index-stays](D-2026-08-25-the-labeller-leaves-the-index-stays.md) | RXNMapper and Rxn-INSIGHT ship to `Chemclaw3-mcp`; the corpus, the drain and the search stay, and the MCP client becomes a kernel primitive because there are now two of them |
| [D-2026-08-25-the-loop-is-a-composite-not-a-template](D-2026-08-25-the-loop-is-a-composite-not-a-template.md) | Multi-step GFN protocols: the fan-out lives in a composite because a template has no loops; structure enumeration goes on `chem` as reads; `crest` ships behind a build flag, and running it found three defects and a per-search `xtb` requirement no documentation states |
| [D-2026-08-25-the-number-was-measured-on-a-path-production-does-not-use](D-2026-08-25-the-number-was-measured-on-a-path-production-does-not-use.md) | The condenser's saving was measured with `model_dump_json()` while LangChain sent pydantic's repr: 2.7x actual against 9.1x claimed. The tool now renders a string, and the upstream assumption is pinned |
| [D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use](D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use.md) | Temporal's LangGraph plugin is declined: its value is a durable `interrupt()` and this system uses none — the human gate is already a Temporal workflow — so it closes neither defect the row attributed to it |
| [D-2026-08-25-the-structure-is-discarded-at-the-note-boundary](D-2026-08-25-the-structure-is-discarded-at-the-note-boundary.md) | A warehouse-ingested protocol reached the graph with no recipe (251 chars in, a 63-char body out); the figures a chemist compares reach the note as frontmatter rather than only as sentences; a share document gets the reader its citation always implied |
| [D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire](D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire.md) | Reviewing the merged GFN work found four templates that could not complete a run, an agent that could not select any of them, and a per-atom average combining different atoms — with CI green throughout; records why each was invisible and that a fake which cannot express the failure is not evidence |
