# Review of the eight-findings change (#280), and the fixes

Seven independent reviewers with fresh context read the merged diff. What they found is below,
in the order it will be fixed. Every item marked **verified** was reproduced by reading the code
or by executing it, not taken on the reviewer's word.

The pattern worth naming before the list: **in almost every case the docstring states the correct
control and the code does not implement it.** That is the exact failure this repository's culture
is built to catch, and one change reproduced it about a dozen times.

## Critical — a control that is claimed and absent

- [ ] **C1** `api/mcp_face.py` serves **zero** tools in production. The registry is populated only as
      an import side effect of `agent/chemclaw_agent.py`, which the face never imports; the five
      tests pass because `tests/test_mcp_face.py` imports it itself. *(verified: `advertised_tools()`
      → `[]` on the production path, 14 with the seeder)*
- [ ] **C2** Irreversible-effect approval is **self-approvable**: `_approve_effect` builds its
      `AwaitRequest` without `asked_of`, so it defaults to `""`, and `_may_answer` returns `True` for
      any authenticated caller including the requester. *(verified)*
- [ ] **C3** `assemble_evidence_pack(session_id=…)` reads **any** session with no ownership check,
      while its docstring claims the check exists. `check_pending_requests` supplies the ids.
      *(verified)*
- [ ] **C4** The `mcp-face` pod has no Service, no Route and is selected by no ingress
      NetworkPolicy — unrestricted ingress under Kubernetes semantics.

## High — corruption, wedges, wrong numbers

- [ ] **H5** `effect_ledger._SETTLE` has no state guard where `_BEGIN` has one, and `_finish()` sits
      inside the `try` after the `applied` settle — so any raise in `_finish` rewrites an applied
      irreversible effect to `failed`. *(verified)*
- [ ] **H6** A re-asked expired question is invisible and permanently unanswerable: `ALLOW_DUPLICATE`
      against an upsert guarded `WHERE state='waiting'` that never resets `state`. *(verified, and
      independently on a live Postgres)*
- [ ] **H7** `external_ref` has **no producer** — `SettleEffectInput` has no such field, so every
      settle writes `""` — while three readers call it the operator's only handle. This is the
      `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` shape. *(verified)*
- [ ] **H8** The unit registry is wrong one prefix past the tests: `nM` → nanometre, `µm`/`um` →
      micromolar, `pM` unknown. `area%` and `% w/w` are bare aliases of `%`, so `basis` is empty and
      they compare **equal**. *(verified by execution)*
- [ ] **H9** Delivery failure is silent (no logger, no metric in `deliver/`) and the activity is
      ordered **before** `acknowledge_digest`, so a non-retryable channel error wedges the watermark
      — contradicting the comment directly above it. *(verified)*
- [ ] **H10** `settings` is read inside workflow bodies (`awaiting.py`, `digest.py`), deciding how
      many commands are emitted — the replay hazard `commitment_sync.py` states the rule against in
      the same commit. *(verified)*

## Medium

- [ ] Evidence pack: silent 200-row truncation with `refusals` counted over the truncated list;
      `state`/`failure_reason` unselected so a failed job reads as a successful one; `is_empty`
      ignores `approvals`; no index on `effects(session_id)`.
- [ ] Operations: `authorship` silently drops `superseded`; `job_activity` counts failures as runs;
      `audit_events.tool` is the model's raw string and reaches a reader undefanged; `distinct_actors`
      is a lower bound described as a count.
- [ ] Deliver/commitments: `Message.kind` is unvalidated and used as a path component;
      `correlation_id` is sent to the webhook host; redaction misses connector bearer tokens;
      one malformed file aborts the whole mirror pass; the commitment cursor shares a row with the
      ELN sync; `commitments-json` hardcodes a CWD-relative path.
- [ ] Pending: `answered_at` is stamped on expiry and cancellation; the losing concurrent answerer
      gets 204; a request routed to an entitlement appears in nobody's inbox.
- [ ] `report_measurement` stamps the ledger's unit on a value the caller never gave one for —
      worse than the empty string it replaced, because the bad rows are no longer identifiable.

## Prose that is false

- [ ] `window.py` justifies the 730-day clamp because those tables "are pruned by
      `durable/retention.py`". Both are in `_NOT_PRUNED`, **"refused"**. *(verified)*
- [ ] "Five tables recorded this system's own work and none had a reader" is false for **four of
      five** — only `turn_costs` had none. Repeated in the ADR, three docstrings, the README and the
      merged PR body. *(verified)*
- [ ] `grants/app_privileges.sql` lists `reaction_records` twice.

## Verification

`make lint type test` green with Docker/Postgres/Temporal up, and the full suite reported with what
it skipped.

## Review

_(filled in at the end)_
