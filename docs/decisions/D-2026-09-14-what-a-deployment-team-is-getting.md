# D-2026-09-14-what-a-deployment-team-is-getting — the production-readiness record for `Chemclaw3`

## Status

Accepted. This is the repository's readiness record: what is **enforced**, what is **bounded**,
what is **measured**, and what is **explicitly accepted** as unbounded or unproven.

## How to read it, and why it is written this way

**Every clause names the test that holds it.** A clause with no test was rewritten as an accepted
risk or deleted — that rule is the document, not a preface to it. This repository's own history is
of prose outrunning its commit: `mcp_servers/calc/` was asserted deleted across four ADRs while
still dispatchable, `audit_events.agent` was documented as naming the agent beside the human on
every row it had never once carried, and the runbook described a `trivy` gate that ran nowhere.
This is the document most likely to be believed without checking, so it is the one that may claim
least.

Three things it deliberately does **not** do. It states no counts that live in a test — a ceiling,
a skip count, a prefix size — because a number in prose is a claim about one commit
(`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`), and every one of those has gone stale
inside a day at least once here. It makes no claim about `Chemclaw3-mcp`, `Chemclaw3_ui` or
`Chemclaw3_mock` beyond what a test in *this* tree reads out of them. And it does not grade: there
is no "ready / not ready" verdict, because that judgement belongs to whoever is accepting the risks
in the fourth section, and a verdict here would let them skip reading it.

## 1. Enforced — the system refuses

| Claim | Held by |
| --- | --- |
| Under `entra_required`, a request with no valid token is refused at the front door, against a real JWKS with nothing patched | `tests/test_entra_end_to_end.py` |
| Every route is behind the authentication dependency, or declared as one of the named exceptions | `tests/test_route_auth_coverage.py` |
| A tool call with no ambient actor is refused rather than run as nobody (`require_actor`) | `tests/test_authz.py`, `tests/test_tool_authz.py` |
| An expensive job is refused for a requester without the entitlement, including a template step launched by another step | `tests/test_authz.py`, `tests/test_template_job_step.py` |
| A plan-gated tool cannot be called under an unapproved plan | `tests/test_plan_gate.py` |
| No agent path writes a `SKILL.md`, and a role-refused skill is absent from the listing, unreadable by path, and unreachable by glob or grep | `tests/test_skill_backend.py`, `tests/test_skill_access.py` |
| `audit_events` is INSERT-only by grant, and the migration ledger is never granted a write verb | `tests/test_database_privileges.py` |
| The grants the runtime role really holds are the matrix the grant file declares, materialised and read back | `tests/test_runtime_ddl_privilege.py` |
| The chart refuses to render until a release states its egress posture, its retention posture and its Temporal namespace | `tests/test_deploy_chart.py` |
| A NetworkPolicy selects peers rather than paths, and every workload an Ingress-typed rule must select is selected | `tests/test_deploy_chart.py` |
| The process refuses to start in the configurations that would silently open a gate — an exposed bind without `entra_required`, a loopback gateway not declared as one, and the rest of that set | `tests/test_config.py` |
| An identity header does not survive a cross-origin redirect: the hook strips everything it stamps, trace context included | `tests/test_connector_identity.py` |
| Every connector this repository hosts authenticates its own `/mcp`; a declared `token_env` that is unset refuses | `tests/test_connector_identity.py` |
| Layering: import direction between packages, and which third-party stack a package may import | `tests/test_layering.py`, `tests/test_third_party_layering.py` |
| A migration is additive; a rollback re-applies its own grant file | `tests/test_migrations_are_additive.py`, `tests/test_database_privileges.py` |
| The dependency closure carries no known vulnerability, blocking, in both workflows | `Makefile::deps-audit`, `tests/test_deploy_chart.py::test_the_dependency_audit_gates_every_branch_push_and_the_local_gate` |
| The built image carries no fixable HIGH/CRITICAL, blocking, on pull requests too | `.github/workflows/image.yml`, `tests/test_deploy_chart.py::test_every_supply_chain_gate_the_runbook_names_actually_runs` |

## 2. Bounded — a ceiling exists and something enforces it

| Claim | Held by |
| --- | --- |
| A model request is bounded: the thread is compacted against a **billed**-token budget that charges the request's own prefix unconditionally | `tests/test_compaction.py` |
| That prefix cannot grow silently: it is observed off the compiled graph with connectors bound, and ratcheted | `tests/test_context_floor.py` |
| A single tool result is bounded head-and-tail, with a notice naming itself as system text | `tests/test_tool_result_size.py` |
| A turn cannot loop forever: a first-party `before_model` counter, so the number that enforces and the number that records are one number | `tests/test_langgraph_agent.py`, `tests/test_middleware_order.py` |
| Concurrent turns cannot pile onto the model endpoint: each takes a permit, the overshoot at the boundary never exceeds the cap, and the wait is reported on the stream | `tests/test_concurrency_claims.py`, `tests/test_service.py` |
| A session's stream count is bounded per user and per process | `tests/test_service.py`, `tests/test_session_events.py` |
| Every dispatched activity bounds its queue wait, and one nobody polls fails instead of waiting for ever | `tests/test_activity_queue_bound.py` |
| A connector request has a wall-clock bound, and the calculation backend's own process group is killed on timeout | `tests/test_connector_transport.py` |
| The fleet's Postgres connection demand is bounded by the chart's own arithmetic, refused at render if exceeded | `tests/test_deploy_chart.py` |
| Retention sweeps delete by declared window, and a deletion cannot orphan a message pair | `tests/test_retention.py`, `tests/test_message_pairing.py` |
| A note prune cannot act on a corpus newer than the one this pod holds | `tests/test_note_index_external.py` |
| A published SSE contract cannot change without the fixture and the OpenAPI document changing with it | `tests/test_event_contract.py` |
| A workflow history written by the previous release still replays | `tests/test_workflow_replay.py` |
| A state channel a hook writes is one the graph declares | `tests/test_state_channels.py` |
| Six shapes upstream never promised are asserted in one file, each naming the module that breaks | `tests/test_upstream_surface.py` |

## 3. Measured — a number somebody can reproduce

| Measurement | Where |
| --- | --- |
| **ChemBench: the full system scores 62/100; the same model with no tools scores 74/100** — 12 points worse, with 20 answers naming no option against 10 | `D-2026-09-14-a-number-somebody-else-can-produce`, `make live-benchmark` |
| Tool use helps about a third of the time and hurts about a quarter, on this repository's own probes | `D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter` |
| Retrieval recall over 46 labelled (query, note) pairs across 20 probes | `make live-probes`, `tests/test_probe_coverage.py` |
| A turn's cost ratio, over turns the system really ran | `make live-turn-cost` |
| The request prefix, observed off the wire rather than re-derived | `tests/test_context_floor.py` |
| Mutation scores for the seven invariant-bearing modules, on a schedule | `.github/workflows/mutants.yml`, `make mutant-results` |
| **The four-tier stack starts and serves, against a real model gateway.** 2026-09-14: `make live-infra`, `make live-up` with `CHEMCLAW_LLM_BASE_URL` naming a gateway, then 331 probes — Postgres, Temporal, four workers, the connector fleet and the front door up; 27 distinct tools exercised across the fleet and core | `make live-probes`, transcripts in `tasks/live-test/transcripts/corpus/` |
| **All four repositories run together.** 2026-09-14: fifteen processes up and answering readiness — `Chemclaw3_mock`'s two, `Chemclaw3-mcp`'s six, this repository's six, `Chemclaw3_ui`'s two. Read back: front door `/readyz` 200, UI SPA 200, `reaction_records` 2103, `session_messages` 1245. Each fleet credential is *enforced* rather than declared, which is what every "credential accepted" line in that lane is | `infra/live/e2e-full-stack/up.sh` |

**The ChemBench number is stated first because it is the only figure in this document that somebody
outside this family produced the questions for, and it is the one that does not flatter the
system.** It is not a defect: a closed-book chemistry question has no evidence in any corpus this
deployment holds, and the agent is told not to answer without evidence, so it declines — which is
the behaviour a process chemist should want and a benchmark scores as wrong. It is stated anyway,
because a readiness record that omits the one external number is exactly the failure this
programme has spent ten waves correcting. What it means for a deployment: **this system is worth
deploying for the work it can cite, and it is measurably worse than the bare model at work it
cannot.**

## 4. Accepted — unbounded, unproven, or out of reach

Each of these is a real gap. None is mitigated by anything in this repository; each is either a
`DEFERRED.md` row with a trigger or a `BACKLOG.md` row with an anchor, and nothing is left implied.

| Accepted | Why it is accepted, and what would close it |
| --- | --- |
| **No cluster has ever run this.** No registry has received a push, no `helm upgrade` has touched a namespace, no `databricks bundle` has reached a workspace. The pipelines exist and every claim they make *about this tree* is checked (`tests/test_jenkins_delivery.py`); nothing here is evidence about somebody's infrastructure | `DEFERRED.md`, "push-to-registry + `helm upgrade` rollout, run". A namespace, a registry and their credentials |
| **A turn's spend is unbounded in the shipped configuration.** `agent/spend_cap.py` exists, is enforced in `before_model`, is metered in `wrap_model_call` and is held by `tests/test_spend_cap.py` — and `agent_max_turn_billed_tokens` ships at **0**, which is off. A deployment that wants the ceiling sets it | Set it. It is off by default because no site's right number is knowable from here |
| **Retention is off in the shipped configuration.** Every `retention_*_days` defaults to 0, so a deployment that states no posture keeps every durable row for the deployment's lifetime. The chart refuses to render without a posture, which makes it a decision rather than a default | State `retention.windows` or `retention.unboundedGrowthAccepted` at release |
| **The browser → tenant hop is unproven.** `tests/test_entra_end_to_end.py` runs the production app against a real HTTP JWKS with nothing patched; what it cannot do is drive MSAL against `login.microsoftonline.com`, and mocking that is mocking a login UI rather than a key set | A real tenant |
| **Two background-worker replicas re-embed the whole corpus.** The prune half is closed (`D-2026-09-14-a-prune-needs-the-corpus-two-pods-disagree-about`); `note_file_fingerprints` is `mtime_ns:size` and two clones of one commit carry different mtimes, so alternating passes re-embed everything. `workers.background.replicas` stays 1 | `BACKLOG.md`, content-derived fingerprints |
| **The `github-actions` dependency closure is audited by nothing.** Actions are pinned by commit, which bounds *what runs* and says nothing about whether it is vulnerable. Measured 2026-09-14: all four actions in use carry zero advisories, so a gate today would be a control with nothing behind it | `BACKLOG.md`, with the OSV query shape named. Trigger: the first advisory on an action here |
| **Two of the four deployables have no chart.** `Chemclaw3_ui` and every `Chemclaw3-mcp` server are deployed by `oc set image` against a Deployment an operator created; a release cannot create one or move a port, a probe, a limit or an env var. The release now says so where it bites | `DEFERRED.md`. One real namespace, and the repository that owns the component |
| **The prefix this system really sends is this repository's ratchet plus what the sibling fleet serves**, and the second half goes stale on somebody else's merge schedule. `SERVED_ELSEWHERE_ALLOWANCE` is the bound; the check skips, loudly, without a sibling checkout | `tests/test_context_floor.py`, `tests/conftest.py::_report_sibling_skips` |
| **A cache miss on one key in different processes still computes twice.** Single-flighting is per process | `DEFERRED.md`, with its trigger |
| **`propose_report` is registered twice**, deliberately, until `background-jobs` drains | `DEFERRED.md`, with the `temporal workflow list` query that makes "drained" observable |
| **`note_proposed` is still accepted by this repository's own probe reader**, deliberately, until every deployment sends the new name | `DEFERRED.md` |
| **A Postgres that accepts the socket and stops answering is not bounded by `/readyz`** — upstream in origin, and `api/routes/ops.py` claimed otherwise until it was measured with `docker pause` | `BACKLOG.md`, with the measurement |
| **No probe *score* has been produced on a real gateway.** The lane ran (above) and the credential's balance was exhausted **17 probes in**: the gateway answered HTTP 400 "credit balance is too low", so 315 of 331 booked `internal`, 15 answered, 1 was empty, and the judge pass failed for the same reason. The 20 recall-scored probes read 0.0 — and that is the refusal, **not** the corpus: `make live-infra` ran first and `note_index` holds 39 rows. What the run does prove is that the stack serves and that a gateway refusal degrades into a failed turn with an error code on the stream rather than into a hang | A credential with balance. Nothing in the tree changes |
| **A local `pytest` run with no database is not evidence about the durable layer.** The Postgres-backed set skips and still prints green; the run's own epilogue counts them and names what it is therefore not evidence about | `tests/conftest.py`'s terminal epilogue. Start the daemon |

## 5. What this document is not

It is not an argument that the system is ready. It is the four lists a person who has to decide
that would otherwise have to assemble by reading the tree, and the fourth list is the one that
decides it.

## What keeps it true

- Every test named in §1–§3 exists and is collected by the suite —
  `tests/test_readiness_record.py::test_every_test_the_readiness_record_names_exists`, which
  resolves each name against the collected suite the way
  `Chemclaw3-mcp`'s `tests/test_decision_log.py` resolves its own "what keeps it true" citations.
  A rename cannot retire a clause in silence, and a clause added with no test cannot be written.
- `tests/test_deferred_register.py` and `tests/test_decision_log.py` hold the two registers §4
  points at.
