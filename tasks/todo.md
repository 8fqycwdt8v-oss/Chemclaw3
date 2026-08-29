# Review of the eight-findings change (#280), and the fixes

Seven independent reviewers with fresh context read the merged diff. What they found is below,
in the order it will be fixed. Every item marked **verified** was reproduced by reading the code
or by executing it, not taken on the reviewer's word.

The pattern worth naming before the list: **in almost every case the docstring states the correct
control and the code does not implement it.** That is the exact failure this repository's culture
is built to catch, and one change reproduced it about a dozen times.

## Critical — a control that is claimed and absent

- [x] **C1** `api/mcp_face.py` serves **zero** tools in production. The registry is populated only as
      an import side effect of `agent/chemclaw_agent.py`, which the face never imports; the five
      tests pass because `tests/test_mcp_face.py` imports it itself. *(verified: `advertised_tools()`
      → `[]` on the production path, 14 with the seeder)*
- [x] **C2** Irreversible-effect approval is **self-approvable**: `_approve_effect` builds its
      `AwaitRequest` without `asked_of`, so it defaults to `""`, and `_may_answer` returns `True` for
      any authenticated caller including the requester. *(verified)*
- [x] **C3** `assemble_evidence_pack(session_id=…)` reads **any** session with no ownership check,
      while its docstring claims the check exists. `check_pending_requests` supplies the ids.
      *(verified)*
- [x] **C4** The `mcp-face` pod has no Service, no Route and is selected by no ingress
      NetworkPolicy — unrestricted ingress under Kubernetes semantics.

## High — corruption, wedges, wrong numbers

- [x] **H5** `effect_ledger._SETTLE` has no state guard where `_BEGIN` has one, and `_finish()` sits
      inside the `try` after the `applied` settle — so any raise in `_finish` rewrites an applied
      irreversible effect to `failed`. *(verified)*
- [x] **H6** A re-asked expired question is invisible and permanently unanswerable: `ALLOW_DUPLICATE`
      against an upsert guarded `WHERE state='waiting'` that never resets `state`. *(verified, and
      independently on a live Postgres)*
- [x] **H7** `external_ref` has **no producer** — `SettleEffectInput` has no such field, so every
      settle writes `""` — while three readers call it the operator's only handle. This is the
      `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` shape. *(verified)*
- [x] **H8** The unit registry is wrong one prefix past the tests: `nM` → nanometre, `µm`/`um` →
      micromolar, `pM` unknown. `area%` and `% w/w` are bare aliases of `%`, so `basis` is empty and
      they compare **equal**. *(verified by execution)*
- [x] **H9** Delivery failure is silent (no logger, no metric in `deliver/`) and the activity is
      ordered **before** `acknowledge_digest`, so a non-retryable channel error wedges the watermark
      — contradicting the comment directly above it. *(verified)*
- [x] **H10** `settings` is read inside workflow bodies (`awaiting.py`, `digest.py`), deciding how
      many commands are emitted — the replay hazard `commitment_sync.py` states the rule against in
      the same commit. *(verified)*

## Medium

- [x] Evidence pack: silent 200-row truncation with `refusals` counted over the truncated list;
      `state`/`failure_reason` unselected so a failed job reads as a successful one; `is_empty`
      ignores `approvals`; no index on `effects(session_id)`.
- [x] Operations: `authorship` silently drops `superseded`; `job_activity` counts failures as runs;
      `audit_events.tool` is the model's raw string and reaches a reader undefanged; `distinct_actors`
      is a lower bound described as a count.
- [x] Deliver/commitments: `Message.kind` is unvalidated and used as a path component;
      `correlation_id` is sent to the webhook host; redaction misses connector bearer tokens;
      one malformed file aborts the whole mirror pass; the commitment cursor shares a row with the
      ELN sync; `commitments-json` hardcodes a CWD-relative path.
- [x] Pending: `answered_at` is stamped on expiry and cancellation; the losing concurrent answerer
      gets 204; a request routed to an entitlement appears in nobody's inbox.
- [x] `report_measurement` stamps the ledger's unit on a value the caller never gave one for —
      worse than the empty string it replaced, because the bad rows are no longer identifiable.

## Prose that is false

- [x] `window.py` justifies the 730-day clamp because those tables "are pruned by
      `durable/retention.py`". Both are in `_NOT_PRUNED`, **"refused"**. *(verified)*
- [x] "Five tables recorded this system's own work and none had a reader" is false for **four of
      five** — only `turn_costs` had none. Repeated in the ADR, three docstrings, the README and the
      merged PR body. *(verified)*
- [x] `grants/app_privileges.sql` lists `reaction_records` twice.

## Verification

`make lint type test` green with Docker/Postgres/Temporal up, and the full suite reported with what
it skipped.

## Review

All of it landed, in five commits. Two migrations (`079_pending_request_run.sql`,
`078_effects_session_index.sql`), one new setting (`effect_approval_role`), one new module
(`agent/tool_modules.py`), and one behaviour change a deployment must know about: **a job declaring
an irreversible effect refuses to run until `CHEMCLAW_EFFECT_APPROVAL_ROLE` names an approver.**
Nothing in this repository declares such a job, so nothing breaks today — the refusal is the point,
because the alternative is the self-approval the seam shipped with.

Three fixes were verified by reverting them and watching the new test go red, rather than by
reading: the empty tool registry, the applied-effect overwrite, and the re-asked question. The unit
ladders were verified by execution, in both directions, at every rung.

**What the audit found beyond the individual defects** is recorded in
`D-2026-08-29-a-review-with-fresh-context-is-a-different-instrument` and in `tasks/lessons.md`:
almost every finding was a docstring stating the correct control beside code that did not implement
it, and the tests that should have caught them were written by the same author and inherited the
same belief — importing the seeder the production path lacked, seeding a marker into columns the
read never selects, checking the two prefixes that happened to work.

**Two claims of my own are retracted** rather than quietly edited: "five tables had writers and no
readers" (false for four of five — each had a point lookup; the *aggregate* was missing) and
`MAX_WINDOW_DAYS`'s justification (it cited a retention that `_NOT_PRUNED` explicitly refuses). The
merged ADRs are untouched, per the rule on merged ADRs; the correction lives in the new one and in
the module docstrings.

**One thing was audited hardest and found sound**: the tool-schema cache, added late under CI
pressure and the change most likely to be wrong. Its stale performance figures are corrected and its
documented-but-unchecked cache bound is now a test.

---

# Second review (fresh context) — the fixes themselves

Six reviewers read the *fix* branch. What they found matters more than the individual defects:

**Two of them mutation-tested the fixes — reverted each and re-ran the suites.** 4 of 5 operations
changes and 6 of 7 deliver/commitments changes survived with the suite green. Most of what this
branch fixed is not pinned by anything. `tests/test_delivery.py` and `tests/test_commitments.py` are
byte-identical to `origin/main`.

**Three regressions were introduced by the fixes themselves**, all in `core/units.py`: `pm`
(picometre) resolved to picomolar because `pM` was added without its twin — turning a safe refusal
into a silent wrong dimension, the *same* defect one rung down, made while fixing the rung above;
`Area%` lost its basis because the basis map was case-sensitive while `parse_unit` is not; and
`% w/v` was registered as a fraction, hard-coding rho = 1.0 g/mL.

**Three of the prose corrections are themselves false.**

## Fixed in this pass

- [x] `pm`/picometre registered; the ladder test now derives from the registry
- [x] `_BASIS_SPELLINGS` looked up case-insensitively
- [x] `% w/v` moved to `mass_concentration` (10 mg/mL, exact by definition)
- [x] `report_measurement` normalises `property_name` — `"PKA"` walked around the new refusal
- [x] the re-ask no longer overwrites an **answered** row's attribution, and refreshes
      `requested_by` — the stale value let a *different* person pass the separation-of-duties gate
- [x] migration renumbered 077 → 079 (main landed its own 077)

## Third pass — the "Still open" list, verified against HEAD

Every item below was checked against the current source rather than taken from the list, because
the list was written against an earlier tree and **nine of the twelve had already landed**. That is
worth naming: a checklist of open findings is state, and state that outlives its closure reads as
live — the same failure `DEFERRED.md` has a test for.

- [x] `get_durable_job_status` — **already fixed**. It is in `WITHHELD` with its reason, and
      `advertised_tools()` measures 8 names, none of them this one. *One residual fixed*: it was
      never added to `test_no_deployment_wide_read_reaches_the_face`'s named set, the second list
      that exists so dropping an entry from the deny-list fails a test saying why it was there.
- [x] the tautological partition test — **already fixed**. `_ADVERTISED` is a golden set and
      `test_the_face_serves_exactly_the_tools_this_list_names` fails on arrival *and* departure.
- [x] `_may_read` has no test — **stale**. `tests/test_evidence_scoping.py` drives it in both
      directions and guards the "return True on a missing row" refactor by name.
- [x] `applied` → `compensated` — **already fixed**. `_SETTLE` carries
      `(state <> 'applied' OR %s = 'compensated')`, and `test_an_applied_effect_can_still_be_compensated`
      asserts the transition the old test could not fail on.
- [x] the deadline clamp — **already fixed, and better than the finding asked for**. The clamp moved
      *into* `open_pending_request_activity` and `due_at` comes back through history, so no launch
      site can skip it and no caller-side copy can drift. The BO comment now points at the activity.
- [x] `_SAFE_TOOL_NAME` — **already fixed** (`^[a-z_][a-z0-9_]{0,63}$`) and the cardinality claim is
      explicitly retracted beside it. *Tests added*, because it had none: the bound had only the
      Postgres-backed injection test, which seeds angle brackets and passes under the loose pattern.
- [x] the `failed` split — **already fixed in prose**. Both `runs` and `failed` now say
      "argument-sets", with `job_records`' upsert-on-`job_id` as the reason.
- [x] the redaction-failure counter — **already fixed**. `_connector_secret_envs` goes through
      `degraded(…, "deliver_redaction", …)`.
- [x] the plaintext-channel refusal — **real, and fixed here.** Measured first: with
      `entra_required=true` and an enabled `http://` channel, `enabled()` returns it, `deliver()`
      returns `[]`, and `make channel-validate` reports **no problems at all**. See the review below.
- [x] a wrong `CHEMCLAW_COMMITMENT_EXPORT_DIR` — **half fixed; the other half done here.** It logs a
      WARNING, so it is not silent in a log; it had no counter, on a failure whose entire symptom is
      silence. Moved onto `degraded(…, "commitment_mirror", …)`.
- [x] the five false-prose claims — **all already corrected**, each as a retraction rather than an
      edit: `window.py`'s "unindexed-range aggregate" (third attempt, and honest — three of four
      tables *are* indexed on the range column), `Coverage`'s retention sentence, `operations/__init__`'s
      "five tables", and `bearer_token_env_names`' `Raises:` block replacing the promised `()`.
- [x] tests for the fixes that survived mutation — the three test files are no longer
      byte-identical to `origin/main`; the gaps that remained were `safe_tool_name` (none at all)
      and the two behaviours this pass changed. Each new test was checked by reverting its fix.

## Review (third pass)

**What was actually broken: one finding of twelve.** `_refuse_plaintext_channel` raises from driver
construction, and construction happens inside `registry.deliver`'s per-channel `try` — the swallow
that keeps one broken channel from costing every other recipient their message, which is correct and
which the digest activity swallows again above it. So the refusal had nowhere to be heard: the
deployment started, looked healthy, and dropped every message with one WARNING each, indistinguishable
from the destination being down. The fix puts the question where it can be answered before anything
is delivered — `plaintext_channel_refusal` returns the reason, the construction site still raises it,
and `make channel-validate` gains it as rule 4, asking every string in the free-form `config:` block
rather than a key spelled `url` (the driver's signature is the schema, so a site's driver may call
its destination `endpoint`). Beside it, `deliver()` now separates *build* from *send*: a channel that
cannot be built fails identically on every message until a manifest is edited, so it belongs on
`chemclaw_degraded_total{subsystem="delivery_channel_config"}` and not inside the outage counter.

**One and a half more were real in the smaller way**: the commitment mirror's missing export
directory had a log line and no counter, and `get_durable_job_status` never reached the second list
naming the deployment-wide reads.

**Nine were stale.** The list was written against the fix branch mid-flight and most of it landed
before this pass began. Verifying rather than trusting cost one measurement each and would have cost
nine unnecessary changes otherwise — several of which (re-tightening a regex, re-adding a clamp at
the launch sites) would have *undone* a better fix that had since replaced the one the finding asked
for. The deadline clamp is the clearest case: the finding asked for the third launch site, and the
right answer was to delete the caller-side clamp entirely and return `due_at` from the activity.

**Verification.** `make lint` and `make type` are green over all 795 files. This pass's own targeted
run covered 327 tests across the changed suites and their neighbours — delivery, commitments,
operations, the metrics and `degraded` label-space suites, the MCP face, evidence scoping, effects,
awaiting, the CLI validators, digest and publish, plus the structural invariants (`test_repo_map`,
`test_layering`, `test_third_party_layering`, `test_upstream_surface`, `test_decision_log`,
`test_deferred_register`) — but did not complete a full `make test` in-sandbox (SIGTERM'd twice at
~18% under a shared, CPU-saturated host). **After all four triage passes merged**, a full `make test`
was run to completion against live Postgres + Temporal on an unshared box: it caught one real
regression (a docstring pointer left dangling by a different section's merge, fixed separately), and
a second full run afterward reported **6250 passed, 0 failed, 14 skipped** (helm absent, truncated
git history, and a briefly out-of-credit live model credential — all pre-existing, unrelated skips).

**Nothing is left deliberately open from this list.** The one thing this pass did *not* do is bound
the `GROUP BY`'s cardinality on `audit_events.tool` — the retraction beside `_SAFE_TOOL_NAME` names
it, says it needs a predicate in the SQL, and says it is a separate change. That judgement stands:
it is a schema-and-query decision about a poisoning burst, not a bounded fix, and the bucketing that
protects the *reader* is in place either way.
