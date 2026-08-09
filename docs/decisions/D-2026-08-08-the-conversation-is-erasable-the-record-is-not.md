# D-2026-08-08-the-conversation-is-erasable-the-record-is-not — what offboarding removes, and what it refuses to

**Status:** accepted

## Context

The extensibility audit asked what the recurring operations cost. Identity came out well as a
*mechanism* — this system has no user table, no local accounts and no invite flow; it reads the
caller's Entra token and nothing else, and every gate reads one flat role set, so adding a role or
entitling a team is pure configuration with zero code. It came out badly as an *operation*:

- `docs/guides/runbook.md` had fourteen numbered procedures — add a skill, a data source, a
  connector, a profile, a template; add a database; cut a release; restore a store — and **none**
  for identity.
- Grepping the whole repository for role-shaped names returned exactly one,
  `chemclaw.sharedrive.reader`, and only because a manifest uses it as an example. An operator
  standing this up had nothing to work from.
- **There was no leaver procedure at all.** Nine tables carry a per-actor column. Removing an Entra
  role stops new access and deletes nothing, and no command deleted anything either. In a system
  that advertises a GxP posture and documents PII in the audit trail (`SECURITY.md`), "someone left,
  remove their data" is a question that will be asked, and the answer was hand-written SQL composed
  under time pressure.
- Revocation latency was undocumented. It is bounded by token lifetime and there is no kill switch —
  defensible, but nowhere written down, so it read as an oversight rather than a decision.

## Decision

**Offboarding splits the nine tables in two, and the line is attribution.**

*Erasable — the conversation.* `session_owners`, `session_messages`, `session_events`,
`session_turns`, `user_preferences`, `subscriptions`. This is how one person worked: private,
revisable, of no interest to anyone else — the same argument `agent/preferences.py` makes for why a
preference is not a knowledge note. None of it is evidence about the chemistry.

*Retained — the record.* `audit_events`, `plan_approvals`, `note_proposals`, `bo_suggestions`,
`job_records`, `turn_costs`. Each says who did what to the science, which is what a GxP system
exists to be able to say. **An attributable record that can be deleted on request is not an
attributable record.** `audit_events` goes further: it carries a tamper-evident hash chain, so
deleting a row does not merely remove information, it breaks the proof that the rows either side of
it were never altered (`make audit-verify`).

`chemclaw.agent.leaver` implements it, `make user-erase ACTOR=<oid> [APPLY=1]` runs it, and runbook
§(xv) documents onboarding, entitling, revoking and offboarding as one procedure.

Three choices inside that are worth stating:

**It reports the retained tier rather than ignoring it.** Counting rows it will not delete, naming
each table and printing why, is the difference between a partial erasure and one that *looks*
complete. An operator whose obligation reaches those rows has a question for the record's owner, and
the report gives them a number to start from. Silence would be the one outcome worse than refusing.

**Dry run by default, and the dry run really deletes.** It executes the statements and rolls back,
rather than running a second counting query. A preview computed a different way from the thing it
previews is a preview of something else — and this is the one irreversible operation an operator
performs on live data whose target is a string pasted from a directory.

**No kill switch, stated as a decision.** Revocation is removing the app role in Entra; an issued
token stays valid until it expires. A deny-list here would be a second source of truth about who may
act, drifting from the directory the first time anyone edited it by hand. Deployments needing faster
revocation have the tenant's levers (continuous access evaluation, a shorter lifetime); deployments
needing an immediate stop scale the front door to zero. Both are now in the runbook.

## Consequences

- The one recurring operation with no written procedure has one, including an inventory of every
  gate that reads a role and the two ways a tenant can express membership (app role verbatim;
  group claim namespaced `group:`, because the same flat set gates tools, skills and shares and an
  unprefixed group would be indistinguishable from an app role).
- `session_owners` gained `DELETE` in `infra/sql/grants/app_privileges.sql` — kept on its own line
  because insert-and-delete-but-not-update matches no other group there, and folding it into the
  full-DML list would hand it an UPDATE its writer deliberately does not use. This gap was found by
  `tests/test_database_privileges.py`, which compares the grant against the writes the code
  performs; the test was already there and did its job the moment the code changed.
- `make audit-verify`, `make share-sync` and `make safety-validate` gained runbook entries too
  (§(xvi)) — all three existed as targets with no document telling anyone to run them, and
  `audit-verify` is what makes the trail evidence rather than a log.
- A residual, stated rather than solved: the retained tier still contains user free text (the audit
  trail bounds each argument to `agent_audit_max_arg_chars` but the excerpt is real content). A
  deployment whose data-protection obligation reaches it needs a retention policy over the audit
  store, which is a deployment decision and not something a CLI flag should quietly make.
