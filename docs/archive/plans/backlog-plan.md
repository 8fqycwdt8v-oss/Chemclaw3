# Backlog implementation plan — assessment before build (2026-07-25)

Source: every open (`- [ ]`) item in `docs/planning/BACKLOG.md`, plus the open rows of `docs/planning/DEFERRED.md` that the
backlog points at. This document does **not** assume a backlog entry deserves to be built. Each
item is assessed first; only the survivors get a spec.

Outcome up front: **31 open items assessed → 8 BUILD, 14 DEFER, 5 DROP, 12 BLOCKED** (some items
split across categories; the table is the authority). Nothing in the BUILD set needs live Entra,
a Temporal cluster, an OpenShift namespace, or a reachable LLM — the whole plan is provable here
under `make lint type test`.

---

## 1. How each item was assessed

Five questions, in this order. The first "no" that matters decides the verdict.

1. **Trigger** — the item names a condition ("at the third ELN source", "when >1 replica exists").
   Does that condition hold *today*, checked against the code, not against memory?
2. **Reality** — does it close a defect or a real capability gap that exists in the tree right now,
   or is it speculation about a future shape? (Evidence must be a file:line, not a narrative.)
3. **Offline-verifiable** — can it be proven green in this environment, or does it need a live
   tenant / cluster / LLM / QM output format? Unverifiable work is not "done", it is hope.
4. **KISS / Rule of Three** — does it have a second real caller, or would it be an abstraction with
   one consumer? Would building it *add* a failure mode (merge conflicts, workflow-history churn)?
5. **Cost vs value** — including the GxP surface and the cost of churning durable Temporal history.

Verdicts:

| Verdict | Meaning |
|---|---|
| **BUILD** | Ships in this plan (wave A/B/C below), with a spec and an acceptance check. |
| **DEFER** | Correct as designed but its trigger genuinely does not hold. Restated with the trigger. |
| **DROP** | Should *not* be built: already done, redundant, or actively harmful. Backlog line corrected. |
| **BLOCKED** | Needs a live edge (tenant/cluster/LLM/format) or a user decision. Not schedulable. |

---

## 2. Verdict table

### Config extensibility (`docs/archive/audit/10-config-extensibility.md`)

| # | Item | Verdict | Why |
|---|---|---|---|
| 1 | Per-extension manifest + enable-list | DEFER | Its own trigger ("skills declaring capability deps, or profile authoring") is unmet: no `SKILL.md` declares a dependency and there is exactly one `AgentProfile` (`agents/profiles.py`). Building it now is a manifest format with no author. |
| 2 | MCP transport `type` union (stdio/HTTP) | DEFER | `McpServerSpec` (`chemclaw/config.py:35`) is stdio-only because every configured server *is* stdio. The union is a ~10-line discriminated-union change the day a remote server exists (the `DataSourceSpec` precedent, D-076); pre-building it buys nothing. |

### OKF-inspired graph polish (D-074)

| # | Item | Verdict | Why |
|---|---|---|---|
| 3 | Per-bundle `log.md` changelog appended by the PR-gate | DROP (as designed) → DEFER (redesigned) | As specified it is a **conflict magnet**: every note lands on its own branch (`kg/git_submitter.py`), so N concurrent proposals all append to the same `log.md` and every one after the first conflicts — we would be manufacturing merge failures to duplicate information git already has. The sound version is a *generated* view (`git log` → rendered changelog), which has no consumer asking for it yet. Redesign recorded; trigger = a reviewer/auditor asks for a non-git changelog view. |
| 4 | External ontology anchoring (ChEBI/RXNO frontmatter) | DEFER | Nothing queries by subsumption today — `kg/graph.py` traversal is wikilink-based and retrieval filters by type/tag. Adding unresolved ontology ids gives an unchecked string field (worse than no field); adding *resolved* ones needs an ontology dump = a new data dependency. Trigger = a retrieval path that queries by class, or an external consumer of our notes. |

### Front door / harness (F2, F3, harness follow-ups)

| # | Item | Verdict | Why |
|---|---|---|---|
| 5 | `PlanEvent` + live `JobStartedEvent` emission (D-042) | **BUILD (B2)** | Both types are **dead code**: `service/events.py:15,37` define them, the union exports them, `service/static/app.js` renders them, and no code path emits them. CLAUDE.md forbids "for later" stubs. Both inputs now exist offline (`agents/harness_todo.py` reads the todo store; `submit_qm_job` already knows the job id at submit). Emit or delete — emitting is the smaller diff and completes F2-T3. |
| 6 | Mid-flight same-turn resume (D-032/D-035 approval seam) | DEFER | Today a long job's result reaches the *next* turn (push-back F3-T3 + the awaiting-todo flip, D-058) — correct, just less slick. Resuming the same streamed turn needs a durable hold **and** an SSE reconnect protocol: a large, UX-only change. Trigger = a real user complaint, or a flow whose turn context cannot be reconstructed next turn. |
| 7 | Plan/loop metrics for Phase 2b | DEFER | Plan-revision and loop-iteration counts are only meaningful against a real LLM driving the harness; offline they measure the scripted client. Same gate as AG-13 — bundle them with it. |
| 8 | Plan-mode approval + finer autonomy behind RBAC | DEFER | The authorization half already landed (F10-C `agents/tool_authz.py` + `agents/authz.py`). What remains is an approval *UX* on top of item 6's seam; it inherits that item's trigger. |
| 9 | Harness ↔ report-pipeline interplay | DEFER | Research question, not a build item. Neither backbone is blocked on the answer. |

### Post-campaign follow-ups (D-072)

| # | Item | Verdict | Why |
|---|---|---|---|
| 10 | ELN late-file detection | **BUILD (A1)** | Real silent data loss: `eln/json_adapter.py:122` drops any file whose payload timestamp predates `since`, and the overlap window (`eln/sync.py:203`) only covers `eln_sync_overlap_seconds`. A file that *appears* late with an old timestamp is dropped forever with **no signal at all**. Detectable offline from `path.stat().st_mtime`. Small, honest, no new config. |
| 11 | Memory cluster merge/shrink supersede | **BUILD (B3)** | Real correctness gap with a GxP edge: `memory/ids.py:26` anchors on the smallest member, which keeps ids stable on *growth* but leaves the loser of a **merge** — and the pre-shrink note of a **shrink** — sitting in the graph as "current" with no supersede link. Two stale-truth notes retrievable as fact. Offline-testable end to end. Also subsumes the manual one-time id-migration cleanup noted in BACKLOG. |
| 12 | `system-eval-drift` consumer surface | **BUILD (A3)** | `workflows/eval_drift.py:76` pushes alerts to a pseudo-session no UI consumes: delivery is guaranteed, *visibility* is not. The KISS close is not a new UI — it is a WARNING on the operator's existing log path plus a documented read procedure. Building a drift dashboard would be over-engineering for one alert type. |
| 13 | Deployment docs (`ENTRA_REQUIRED`, removed `ENTRA_CLIENT_ID`) | **BUILD (A2)** | Pure documentation of two already-shipped behaviors (D-067 refuse-to-boot; `extra="forbid"` rejecting a stale env var). Cheapest item in the backlog and it prevents a boot failure an operator cannot diagnose. |
| 14 | Substructure match compute bound | **BUILD (B1)** | The remaining half of a guard already half-built: query length and scan size are bounded (`mcp_servers/molfp/search.py:79,90`) but the match loop runs **on the event loop**, so one adversarial recursive SMARTS from the model stalls the whole front door, not just its own call. `asyncio.to_thread` + a wall-clock bound is small and offline-testable. |
| 15 | Workflow versioning policy before first live deploy | **BUILD (C1, policy only)** | The *policy* is writable today and is the cheapest possible insurance against a whole class of nondeterminism bugs on the first production deploy. What is **not** worth building is a CI guard that greps workflow diffs for `workflow.patched()` — it cannot tell a logic change from a docstring edit, so it would train people to bypass it. Doc + deploy checklist only. |

### Resilience / scale deferrals (D-066, D-072)

| # | Item | Verdict | Why |
|---|---|---|---|
| 16 | Durable / rolling-window budget quota | DEFER | `service/budget.py` closes the in-process runaway, which is the failure mode we actually have. A restart-surviving, cross-pod quota needs a Postgres-backed windowed counter and only pays off under multi-tenant billing pressure that does not exist. Trigger unchanged. |
| 17 | Substructure pattern-fingerprint prefilter | DEFER | Its trigger is explicit and measurable: the truncation warning firing in real use. The corpus is far below the 5000-record scan cap. Also note ECFP bits cannot screen substructures soundly, so this is a new indexed column, not a tweak. |
| 18 | Multi-process note-submit serialization | DEFER (+ one-line guard in A2) | The floor rose since the entry was written: D-069 added an OS-level advisory checkout lock and a dedicated-checkout guard, and the shipped chart runs the background worker at `replicas: 1` (`deploy/helm/chemclaw/values.yaml:32`). The exposure is a values.yaml edit away, so A2 pins the reason in a comment rather than building distributed locking for a replica count nobody set. Trigger = background replicas > 1. |
| 19 | `within=` id-array scaling, `XtbInput.charge`, open-shell species, JS test infra (D-072) | DEFER | Re-checked; each trigger is a scale or format change that has not happened. No action. |

### Capability gaps (`docs/archive/research-review.md`, `docs/archive/plans/parity-plan.md`)

| # | Item | Verdict | Why |
|---|---|---|---|
| 20 | **Chemical/biological safety layer** | **BUILD (C2) — scope needs sign-off** | The one gap the user explicitly parked *for a decision* rather than deferring, and the only large item here that is **not** infra-gated. The agent can already propose procedures and BO candidates with zero hazard awareness, and the backlog itself says decide "before any capability phase that could propose a hazardous route" — that phase already shipped (1d BO recommendations, 5b reports). A deterministic, advisory-only slice is buildable offline. Scope confirmation requested in §5. |
| 21 | Gate-until-trigger parity items (OCR/vision, Veeva/SAP/LIMS connectors, GAMP-5 artifacts, multi-agent mesh) | DEFER | Each has a written trigger in `docs/archive/plans/parity-plan.md`; none holds. The mesh in particular stays declined on KISS grounds (one agent + skills + Temporal fan-out covers it, D-030/F10-D). |
| 22 | Retrosynthesis · lab automation/SiLA2 · flowsheet synthesis · multimodal analytical data · domain foundation models | DEFER (confirmed) | Re-examined as the backlog asks ("confirm or pull forward"). All five need either a physical/vendor integration or a model+license decision, and none is on the critical path. Confirmed as-is. |
| 23 | Design caution: apply skills/tools selectively, measured per task | DROP | Already implemented as designed: `evals/ab.py` (2b.4) measures per-task tool utility including cases where tooling hurts, and `AgentProfile` (D-075) narrows the toolset per use case. Nothing left to build; the caution is satisfied. |
| 24 | Design caution: design the memory layer against DMR/LongMemEval | DEFER | Would require importing an external benchmark and a live LLM to score it. Bundle with AG-13. |

### Foundation phases F0–F7

| # | Item | Verdict | Why |
|---|---|---|---|
| 25 | F0-T4 tool-calling spike | DROP the stand-in half · BLOCKED for the live half | The spike's only value is proving the *internal endpoint's* tool-calling fidelity. A stand-in OpenAI-compatible server would prove our client wiring — which `tests/test_harness_execution.py` (F1-T4, D-058) already proves against a real `FunctionInvocationLayer`. Building the stand-in would test the stand-in. Keep the ticket, blocked on the endpoint. |
| 26 | F4 live edges (real token validation, federation/OBO exchange, live Temporal mTLS) | BLOCKED | Needs a real tenant/broker. Code + fake-endpoint tests are green (D-044…D-047). |
| 27 | F4-T5 remainder: "per-request role → skills scoping" | **DROP (stale — already done)** | Delivered by D-052: `agents/skill_access.py::RoleScopedSkillsSource` filters per request off the turn's ambient roles (`agents/identity_context`) and is wired at `agents/chemclaw_agent.py:139` with `settings.skill_role_gates`. The backlog line is out of date; corrected in this change. |
| 28 | F5 deferred: `QMJobWorkflow` → `CalculationWorkflow` rename | **DROP** | Cosmetic, high-churn, and *actively harmful*: the workflow type name is part of durable history, so renaming it is precisely the un-versioned change item 15's policy exists to forbid. Not worth a migration for a nicer noun. |
| 29 | F5 deferred: real `cclib` parsing · live-cluster durability spike (CHECKMATE 1) | BLOCKED | Needs a fixed live QM output format and a running cluster respectively. |
| 30 | F6 live edges (image build/push, `helm template` + `kubeconform`, dry-run rollout, OTel collector, ExternalSecret) | BLOCKED | CI/cluster-gated by definition. |
| 31 | F7 deferred: Snowflake ELN connector · LIMS/MES/analytical/literature adapters | BLOCKED | Needs the internal pipeline/tenant. The seam is ready (D-050/D-076) — a connector is one registry entry when the source exists. |

### Older tails

| # | Item | Verdict | Why |
|---|---|---|---|
| 32 | 1b.5 Temporal lookup/persist activities | **DROP (stale)** | Folded into 1c.5 by design to avoid a stub; the checkbox was never cleared. Corrected. |
| 33 | 1c.3 GNN solubility model | BLOCKED (user input) | Needs the model choice + weights license. The `run_cached` contract makes the swap cheap once chosen. |
| 34 | 1c.7 fast-calc graph note via PR-gate | DEFER | Two publishers already exist (QM 2.8, BO 1d.5); a third near-identical mapper before anyone asks is exactly the abstraction-without-a-caller CLAUDE.md forbids. |
| 35 | AG-13 agent-behavior/prompt/skill regression eval · F10-B3 report-prose faithfulness · audit-chain tip-truncation anchor · live-retriever drift | BLOCKED / DEFER | Each is documented in `docs/planning/DEFERRED.md` with a trigger that is a live LLM, an in-workflow prose step, a regulator requirement, or a populated deployment graph. Re-checked; unchanged. |
| 36 | Open questions (pKa vs PK/ADMET · model choices · first real BO case · Temporal vs Restate/DBOS · Markdown→Neo4j tipping point · SiLA2 wiring · safety-layer scope) | BLOCKED (user input) except the tipping point | All need the user or a real deployment. One improvement proposed in §5: give the Neo4j question a *measurable* trigger (traversal p95 or ~10⁵ notes) instead of leaving it open-ended. |

---

## 3. Build plan

Eight items in three waves. Waves are ordered by risk-of-being-wrong, not by size: A is
independently mergeable and touches almost nothing; B changes behavior in three subsystems;
C needs a decision before code.

Every item ships as its own commit, green under `make lint type test`, with tests that prove
behavior (not mocks of the thing under test).

### Wave A — silent failures made visible (all [S], no behavior change)

#### A1 — ELN late-file detection
*Problem.* `JsonExportAdapter.fetch_new_entries` (`eln/json_adapter.py:112-131`) keeps entries with
`created >= since` and silently discards the rest. `_fetch_floor` (`eln/sync.py:203`) rewinds
`since` by `eln_sync_overlap_seconds` to catch entries written slightly late, but a file that lands
*after* that window with an older payload timestamp is dropped on every subsequent run, forever,
with no log line, no rejection, and no counter.

*Design.* In the same loop, when `created < since`, compare the file's modification time:
`path.stat().st_mtime >= since` means the file appeared after the cursor was set — it is a genuine
late arrival, not old data we already ingested. Collect these and emit **one aggregated WARNING per
fetch** (count + the first ten names + the exact recovery: re-run the sync with an explicit earlier
`since`). Aggregated rather than per-file because a permanently-late file re-warns on every sync;
one bounded line per run stays readable, an unbounded per-file storm does not.

*Scope.* Both file adapters have the identical shape — `eln/json_adapter.py:112` and
`eln/ord_adapter.py:84` each `glob("*.json")`, parse a timestamp, and keep `created >= since` — so
the check goes in one small helper (in `eln/adapter.py`, beside the contract) with two real callers,
not copied twice. No new config: `since` and the mtime are already in hand.

*Tests* (`tests/test_eln.py`): a fixture directory with (a) a current file, (b) an old file with an
old mtime — silent, correctly ignored, (c) an old file with a fresh mtime — warns. Assert via
`caplog` that only (c) warns, that the returned entry list is unchanged in all three cases, and that
the message names the file and the recovery.

*Acceptance.* A late-arriving export is impossible to lose without a log line saying so.

#### A2 — deployment documentation + the background-replica pin
*Problem.* Two shipped behaviors are undocumented and both fail at boot: an exposed deployment must
set `CHEMCLAW_ENTRA_REQUIRED=true` (D-067 refuses to start otherwise), and a still-exported
`CHEMCLAW_ENTRA_CLIENT_ID` now fails startup under `extra="forbid"`. Separately, item 18's exposure
(per-process note-submit serialization) is one values.yaml edit away with nothing recording why.

*Design.* `docs/guides/runbook.md`: a short "exposed deployment" subsection with both env-var facts and the
exact error text an operator will see. `deploy/README.md`: the same two lines in its config/secret
section. `deploy/helm/chemclaw/values.yaml:32`: a comment on the background worker's `replicas: 1`
— note submission serializes per host (D-069 advisory lock), so raising this needs the distributed
lock in BACKLOG first.

*Acceptance.* Grep-able; docs-only, no test. `make lint` unaffected.

#### A3 — eval-drift alert visibility
*Problem.* `workflows/eval_drift.py:76` pushes each `DriftAlert` to the `system-eval-drift`
pseudo-session through the must-deliver `notify_session` seam, so a dropped alert fails the run —
but nothing consumes that session, so a *delivered* alert is equally invisible.

*Design.* Log each alert at WARNING at the point of emission (metric name, baseline, observed,
`vanished` flag), so drift lands on the same operator log path as every other WARNING. Document in
`docs/guides/runbook.md` how to read the backlog of alerts (`session_events` filtered to the channel) and
state that no UI consumes the channel *by design* — the log is the surface until a deployment asks
for more. Explicitly **not** building: a drift dashboard, an email/webhook fan-out.

*Tests* (`tests/test_eval_drift.py`): assert the WARNING is emitted per alert alongside the existing
must-deliver push, and that a run with no drift logs nothing.

### Wave B — real defects ([S]–[M], behavior changes)

#### B1 — substructure matching off the event loop
*Problem.* `find_substructure_matches` bounds its query length and its scan, then runs
subgraph-isomorphism matching **synchronously inside an async function**. SMARTS matching is
worst-case exponential, so one adversarial recursive pattern from the model blocks the front door's
event loop — every other session's stream stalls, not just the offending call.

*Design.* Move the match loop into `asyncio.to_thread` and bound it with `asyncio.wait_for(...)`
using a new `substructure_match_timeout_seconds` (default 5.0, in the fingerprint config section +
`.env.example`, ENV-overridable like every other threshold). On timeout raise `FingerprintError`
with an actionable message (the pattern, the bound, "narrow the fragment").

*Honest limitation, to be documented in the docstring:* `wait_for` unblocks the *caller*; it cannot
kill the RDKit thread, which runs to completion holding one CPU. This bounds the event loop and
caller latency, not total CPU. Killing the work needs a subprocess — over-engineering until a
measured abuse case exists.

*Tests* (`tests/test_molfp.py`): the normal path returns identical results (regression); a
monkeypatched slow matcher trips the timeout and raises `FingerprintError`; the event loop stays
responsive during a slow match (a concurrent task makes progress) — the property that actually
matters.

#### B2 — emit `PlanEvent` and `JobStartedEvent` (closes F2/F3-deferred, D-042)
*Problem.* Two of the seven typed turn events are never emitted. `service/static/app.js` renders
them, so the UI silently shows nothing between "you asked" and "here is the answer" for both the
harness plan and a launched job — the exact "watch the agent work" experience F2-T3 was written for.

*Design.*
- **`JobStartedEvent`** — a per-turn sink via the established ambient-contextvar idiom
  (`agents/session_context.py` is the precedent): a new `agents/job_events.py` holds a contextvar
  list; `submit_qm_job` appends `(job_id)` right where it already calls `mark_awaiting_job`
  (`agents/harness_todo.py`); `service/runner.py` drains the sink inside its update loop and yields
  a `JobStartedEvent` per entry. A plain list, not an `asyncio.Queue` — the runner drains
  synchronously between updates and nothing blocks on it.
- **`PlanEvent`** — when `harness_enabled`, read the todo list through the same `TodoSessionStore`
  access `agents/harness_todo.py` already uses, and yield a `PlanEvent` **only when the list changed**
  since the previous emission (no repeated identical plans in the stream).
- No new config, no new event types, no protocol change: the union and the client already handle both.

*Tests* (`tests/test_runner.py` / `tests/test_service.py`): a fake agent whose tool call publishes a
job id yields exactly one `JobStartedEvent` with that id, in order, before the answer; a harness turn
whose todos change mid-turn yields one `PlanEvent` per distinct list and none when unchanged; the
classic (non-harness) path yields no `PlanEvent`. ADR **D-077**.

#### B3 — supersede memory notes on cluster merge and shrink
*Problem.* `stable_id` (`memory/ids.py:26`) anchors a campaign/playbook note on its cluster's
smallest member id. That is exactly right for *growth* (the note updates in place through the
idempotent PR-gate branch), and wrong for two other transitions:
- **merge** — clusters A and B become one, whose anchor is one of the two old anchors; the *loser's*
  note stays in the graph as current knowledge describing a subset that no longer exists;
- **shrink** — the cluster loses its smallest member, so a new id is minted and the pre-shrink note
  stays current beside it.
In both cases retrieval can cite a stale note as fact, with no link to the note that replaced it.
Under GxP this is the failure mode the bi-temporal fields exist to prevent.

*Design.* One shared helper — `memory/supersede.py`, used by all three synthesis jobs in
`memory/jobs.py` (`synthesize_campaigns`, `distill_playbooks`, `synthesize_optimization_campaigns`
— three real callers, comfortably past the Rule of Three):

1. After a run computes its clusters and their ids, load the merged notes of that type
   (`kg.graph.load_notes`) and read each one's cited member ids (`kg.note.cited_ids` — already shared
   with the F10-B verifier, no new parsing).
2. Any existing note whose member set **intersects** a new cluster but whose **id differs** is
   superseded by the new note.
3. Propose an update to the superseded note through the *same* PR-gate: set `valid_to` = today
   (`kg/note.py` already validates `valid_to >= valid_from`, F10-G2) and append a one-line body
   note naming the replacement.
4. The replacement is named as **plain text, not a `[[wikilink]]`**. A wikilink to a note that is
   still an unmerged proposal would fail `kg-validate` if the reviewer merged the supersede PR first
   — an ordering trap for a human. The edge buys nothing: a note with `valid_to` in the past is
   already excluded from retrieval as non-current (`kg/note.py:129`).

*Bonus.* This subsumes the one-time manual cleanup BACKLOG records for notes minted under the old
set-derived ids: such a note intersects its successor's members with a different id, so the first
run after this ships supersedes it automatically.

*Tests* (`tests/test_memory.py`): merge (two prior notes, one new cluster → the loser gets a
`valid_to` proposal, the winner updates in place); shrink (new id + the old note superseded); pure
growth (unchanged — the existing in-place test must still pass, proving no regression); an unrelated
note with no member overlap is untouched. ADR **D-078**.

### Wave C — needs a decision first

#### C1 — workflow versioning policy [S, docs only]
*Problem.* The 2026-07 campaign changed workflow logic (fan_out's local activity, `ElnSyncWorkflow`'s
chunk loop, BO activity seed args) with no `workflow.patched()` gates. Safe **only** because no live
cluster holds in-flight histories — a fact that stops being true on the first production deploy, at
which point replay of an in-flight history against changed code is a nondeterminism failure that is
painful to diagnose after the fact.

*Design.* `docs/guides/workflow-versioning.md`: what counts as a logic change (control flow, activity call
order/arguments, timers, new/removed steps) versus what does not (docstrings, activity *bodies*,
type hints); the two sanctioned responses — gate with `workflow.patched()`, or make
drain-in-flight-runs an explicit deploy step; the drain procedure; and a statement of today's state
(no live histories, so the past un-gated changes are safe and need no retroactive patch). Add the
check to the deploy checklist in `deploy/README.md` and cross-link from `docs/guides/runbook.md`.

**Deliberately not built:** a CI guard that flags workflow-file diffs lacking `workflow.patched()`.
It cannot distinguish a docstring edit from a control-flow change, so it would fire constantly and
teach people to bypass it — a guard that trains its own defeat is worse than a checklist. ADR **D-079**.

#### C2 — chemical safety screening, minimum viable slice [L] — **confirm scope before building**
*Why now.* This is the only large non-infra-gated gap left, and the condition the backlog itself set
("decide scope before any capability phase that could propose a hazardous route or procedure") is
already past: BO recommendations (1d.5) and development reports (5b) both publish agent-authored
procedures today with **zero** hazard awareness anywhere in the tree (no hazard/GHS logic exists —
only prose cautions inside two `SKILL.md` files).

*Proposed scope (deterministic, advisory, offline).*
- `safety/screen.py` — `screen_structure(smiles)` and `screen_reaction(reaction_smiles)` returning
  `HazardFlag(rule_id, severity, explanation, citation)`; matching runs over a **committed SMARTS
  rule table** (`safety/rules.yaml`): energetic/unstable motifs (azide, acyl azide, diazo/diazonium,
  peroxide/peracid, polynitro, perchlorate), plus a small incompatible-pair table (strong oxidizer
  with strong reductant, etc.). Every rule carries a literature citation — the same evidence
  discipline the knowledge graph enforces.
- An agent tool `screen_hazards` registered through the `@tool` registry (D-075) — one decorator, no
  `build_agent` edit.
- `skills/safety-screening/SKILL.md` — the judgment layer: flags are **advisory input to a human**,
  never a clearance; a proposed procedure must carry its flags; escalate to EHS.
- A `kg-validate` rule: a proposed procedure-type note whose structures flag at/above a configured
  severity must carry a `## Hazards` section. Enforced in `kg/validate.py` (already in CI) rather
  than in the submitter, so the gate stays in one place.
- One `@metric` over a small committed labelled case-set, so the rule table's recall is measured
  rather than asserted.

*Explicit non-goals* (each is a separate decision, none is snuck in): no GHS/SDS database (licensing),
no toxicity/ADMET prediction, no route-level "this synthesis is safe" verdict, no regulatory claim.
**The system flags; it never certifies.** A screen that returns no flags must be rendered as "no rule
matched", never as "safe" — an over-trusted screen is more dangerous than none.

*Cost.* ~2–3 days including the rule table and its citations.

**Decision needed before implementation** (three questions, in §5).

---

## 4. Sequencing, verification, rollout

| Wave | Items | Depends on | Gate |
|---|---|---|---|
| A | A1, A2, A3 | — | `make lint type test` green; new tests in `test_eln.py`, `test_eval_drift.py` |
| B | B1, B2, B3 | independent of A and of each other | as above + `test_molfp.py`, `test_runner.py`/`test_service.py`, `test_memory.py` |
| C | C1 (docs) · C2 (after sign-off) | C2 alone needs the §5 answers | as above + `make kg-validate` for C2's new rule |

- One commit per item; each self-contained and revertable.
- CHECKMATE (G1–G7) after wave B and again after C2 — B3 and C2 both touch the GxP surface.
- ADRs: **D-077** (event emission), **D-078** (memory supersede), **D-079** (workflow versioning),
  **D-080** (safety screening, if C2 is approved). Wave A items are too small for ADRs and are
  recorded in `docs/planning/BACKLOG.md` on completion.
- `docs/planning/BACKLOG.md` and `docs/planning/DEFERRED.md` updated at the end of each wave; the DROP verdicts (items 3, 23,
  25-stand-in, 27, 28, 32) are corrected in `docs/planning/BACKLOG.md` as part of this change, since they are
  claims about the tree that are currently false.

## 5. Open decisions for the user

1. **C2 scope** — (a) advisory-only screening as specified, or a broader safety layer? (b) committed
   SMARTS rule table, or is an external hazard database (with its licensing) in scope? (c) should the
   `kg-validate` hazard-section rule **hard-fail** CI, or warn for a probation period?
2. **Item 36 / Neo4j tipping point** — propose replacing the open-ended question with a measurable
   trigger (graph traversal p95 above a threshold, or ~10⁵ notes) so it stops being re-litigated.
3. **Item 20 vs everything else** — C2 is the largest piece of work here by an order of magnitude.
   If capacity is tight, waves A+B are worth landing on their own; they are independent of it.
