# D-044 — F4-T3: the core rule — user-triggered workflows are user-specific via `require_actor`

**Context.** The mandate is "every backend workflow is user-specific via Entra (required,
authorizing, reject-if-absent)." Taken literally that means a required `requested_by` oid on every
workflow input. But two facts shape the honest implementation: (1) only two workflows have a **live
agent-tool trigger** today — `submit_qm_job` and the interaction-approval — the BO campaign, report,
and memory workflows have no user-facing trigger yet; (2) the memory-distillation and ELN-sync
workflows take **no user input at all** — they are scheduled/background jobs, not launched by a
person.

**Decision.**
- **One reusable guard.** `agents/authz.py::require_actor()` is the single place the rule flows
  through: it returns the turn's ambient Entra oid, and under `entra_required` **rejects** a
  user-triggered workflow with no authenticated user (`AuthorizationError`) *before* any durable
  work — mirroring how `require_canonical_smiles` rejects bad data at the durable boundary. In dev
  (no tenant) it returns the configured `service_actor_id` (replacing the old magic `"unknown"`).
- **Wired into the one live user-trigger.** `submit_qm_job` now populates `requested_by =
  require_actor()`, so the reject-if-absent rule is enforced there. `requested_by` stays out of
  `qm_job_key` (D-011: cache identity is molecular, not per-user; two users share one cached compute).
- **No speculative fields.** Adding a required `requested_by` to `CampaignSpec`/`ReportRequest` now —
  with no caller to populate it — would be a dead "for-later" field, which CLAUDE.md forbids. Those
  inputs adopt the same `require_actor()` guard when they gain live triggers (a later phase).
- **System jobs are not user-specific by design.** Scheduled ELN-sync and memory-distillation run as
  the service, not on behalf of a person; they never call `require_actor`. Attributing them to a user
  would be wrong. The rule is precisely: every *user-triggered* backend workflow is user-specific.

**Consequence.** The core rule is real and enforced at the only live trigger, via one reusable piece;
the mechanism is ready for every future user-trigger; no dead code; the science-dedup cache is
untouched. Tested offline: ambient-user attribution, dev fallback, and reject-if-absent (both the
guard directly and through `submit_qm_job`, independent of the role gate).
