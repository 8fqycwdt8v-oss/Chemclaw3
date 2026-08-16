# D-2026-08-16-an-announcement-is-not-a-failure — eight backlog rows, and the two that were one mistake twice

**Status:** accepted · **Date:** 2026-08-16

## Context

A batch of `BACKLOG.md` rows, each small enough that the interesting part is not the fix. Two of
them turn out to be the same mistake made in two places, and that is what this ADR is for; the rest
are recorded so the reasoning behind each is not reconstructed from a diff later.

## The decisions

### 1. A capability announcement is not a turn failure (`evals/live.py`)

`ProbeOutcome.failed_loudly` existed to answer one question: did a turn that produced nothing *say*
that something went wrong. Its opposite — answered nothing, said nothing — is the silent death the
first live pass found and that no scripted test can reach.

`degraded` was folded into it. Then the runner began probing Temporal before every turn, so any
deployment without a broker announced `durable-jobs (Temporal)` on **every single turn**, and
`failed_loudly` was true everywhere. The harness's most important number became a constant, and it
reported as a clean zero rather than as an error.

**A degradation announcement is made before the turn runs anything.** It names a capability the turn
will not have — the system working, and announced deliberately so the model plans against the
surface it will actually get. A turn failure is something that went wrong while doing the work.
`failed_loudly` now reads `tools_failed or error_code`, and nothing is lost: `degraded` and
`first_degraded_index` are their own fields and `_degradation_findings` grades the announcement on
its own terms. A turn that announces an outage and then dies producing nothing is exactly the silent
death this looks for, not an exception to it.

### 2. The same omission, one workflow over: a failed template said nothing

`ConnectorJobWorkflow._notify_failure` already existed, added after a live pass found a job that
died 30 seconds after its turn ended with no event of any kind. `TemplateWorkflow.run` had the
completion push-back and no counterpart, so a template that failed at step 2 of 5 ended in silence —
same defect, same shape, never carried across.

The fix is deliberately the same shape rather than a better one: same best-effort stance (the run is
already failing; a push-back that failed on top would replace one lost message with two), same
`job_failed` kind. It adds `step`, because "the template failed" is unactionable for a procedure
with several of them and the step id is the one thing this workflow knows that the failure does not.

**The general point both rows make:** a fix applied to one workflow is not a fix to the *class*. Two
sequencers with the same obligation had it stated in one of them, and the second one's absence was
invisible because every test exercised its success path.

### 3. An inverted campaign is right in every number and backwards in its conclusion

A durable campaign carries its direction twice — in `CampaignSpec.problem.objectives[0].direction`,
which is what BoFire optimizes, and implicitly in what the *registered* objective means — and
nothing compared them. `objective_name="solubility_max"` with `direction="minimize"` ran to
completion, spent the full evaluation budget, wrote a PR-gated `bo-candidate`, and recommended the
**least** soluble molecule as its best point.

Nothing in that note is false. The conditions were evaluated, the value is what the model computed,
and the campaign found the extremum it was asked for — which is what makes it the class of wrongness
a reviewer can least catch. The registry now declares each objective's direction
(`RegisteredObjective`) and `require_campaign_startable` refuses the mismatch at launch, before a
budget is spent. `registered_direction` is split from `get_objective` so the check costs nothing: it
must not fit a surrogate or construct a calculator client to refuse a campaign.

### 4. Two schema disclosures that read as ordinary diagnostics

`WarehouseQueryError`'s message carried the driver's own text, and Snowflake's `ProgrammingError`
quotes the failing statement. That error is marked non-retryable by class name, so raised inside a
durable job its message reaches the session — putting the site's table names, its column names and
the shape of the query the binding built into a chemist's transcript and the model's context.

The replacement is not less information but *different* information: the error number and the query
id, which locate the statement in the warehouse's own query history where the person debugging it
already has the access to read it. The full text is one `logger.exception` away, on the pod. The
test asserts both halves — the secret is absent from the message **and** present in the log — because
redaction that loses the detail is a second defect rather than a fix.

### 5. `UnicodeDecodeError` is a sibling, not a child

`ord_adapter` caught `(OSError, json.JSONDecodeError, OrdFormatError)` and promised skip-and-continue.
`UnicodeDecodeError` derives from `ValueError`, so it is `json.JSONDecodeError`'s *sibling* rather
than its parent, and it is not an `OSError` — the file opens and reads fine, the bytes are simply not
UTF-8. One export written by a tool that emitted latin-1 therefore aborted the whole directory. The
test's bad file sorts first, so under the defect the good one is never reached at all.

### 6. Two CI checks that were not running

- **Migration number collisions.** `037` and `043` each name two files. Decided: **do not
  renumber** — the runner applies and records by filename, so nothing is broken today, and renaming
  a merged migration is the destructive edit `test_no_merged_migration_had_its_statements_changed`
  refuses (and would make every database that recorded the old name apply the new one again). The
  four are grandfathered *pairwise* by name, so a third file claiming `037` still fails, and adding
  a fifth name to the exemption set is a visible act in a diff.
- **`fetch-depth: 0`.** On the `actions/checkout@v4` depth-1 default the commit that introduced a
  migration *is* the graft, which is `HEAD`, so `git show <introduced>:file` returns the working
  tree's own content and every migration compared equal to itself. The test already skips honestly
  rather than passing; this one line is what makes it actually run.

  **And the first time it ran, it found two** — `002_molecule_fingerprints.sql` and
  `003_reaction_fingerprints.sql`, both with their `CREATE TABLE` edited in place after merge
  (`smiles` → `label`, plus a `definition` column). That is the check working on its first
  execution, not a regression, and it is recorded here because the *resolution* is a judgement
  rather than a repair.

  The edit was deliberate and is documented in the tree: `004_fingerprint_definition.sql` opens with
  "Fresh databases get the column straight from 002/003; this migration brings an existing dev
  database up to date." Someone added the column to both `CREATE TABLE`s *and* wrote the `ALTER` for
  databases that had already run them. Under `D-2026-08-04-the-schema-only-goes-forward` only the
  second half is allowed — but that rule, and the checksum guard that enforces it, both postdate the
  edit.

  **Reverting would break every database that exists in order to fix one that cannot.** The ledger
  keys on the checksum recorded when a file was applied. A database that ran 002 *before* the edit
  already fails `make db-migrate` today, and is unreachable regardless: that version named the
  column `smiles`, nothing ever renamed it, and no current query would find it. Every database
  created since recorded the current checksum, and restoring the old statements would make the
  runner refuse on all of them. So the two are named exemptions with the reason stated, exactly as
  the collision check grandfathers `037`/`043` — and
  `test_no_grandfathered_edit_outlives_its_reason` fails if either stops being an edit, so the
  permission cannot outlive what it was granted for. The teeth are intact for everything after:
  appending an `ALTER TABLE` to `006_audit_events.sql` turns the check red, measured.

## Consequences

- A live eval run can now report a non-zero silent-failure count, and a broker-less run no longer
  reports every turn as loudly failed. Any historical run's `failed_loudly` figure is not comparable
  to a new one — it was measuring the deployment, not the turn.
- Adding an objective to `_REGISTRY` now means declaring its direction.
  `test_every_registered_objective_declares_a_direction_the_vocabulary_allows` iterates the registry
  rather than a list, so a new row is covered on the day it is added — and a row spelling it `"max"`
  fails there instead of refusing every campaign that names it.
- `require_campaign_startable` imports `science.bo.objectives` *inside* the function, because
  `objectives` imports this module. Stated in the docstring rather than left to be rediscovered:
  moving the check into `objectives` is not the alternative it looks like, since `connector.yaml`
  names exactly one `precondition` and `cli/validate_connectors.py` checks its signature.

## Verification

Every one of the six code fixes was mutation-checked — the fix reverted, the specific test observed
red, the fix restored:

| mutation | test that caught it |
|---|---|
| re-add `or outcome.degraded` | `test_an_announced_outage_does_not_hide_a_silent_death` |
| drop `_notify_failure` from the step loop | `test_a_failed_template_step_wakes_the_session_and_names_which_step` |
| restore the driver's text in the message | `test_a_rejected_statement_reaches_the_caller_without_the_query_in_it` |
| drop `UnicodeDecodeError` from the `except` | `test_one_non_utf8_ord_export_does_not_abort_the_directory` |
| add a second `045_` migration | `test_no_two_migrations_claim_one_number` |

The template test runs against a **real** Temporal server, not a direct call to `run`: the behaviour
under test is on a workflow's failure path, which is where the SDK's own handling of an exception
raised in workflow code lives. Its first form hung for 30 seconds because the push-back activity was
scheduled to a queue no worker polled — a passing-looking timeout, which is why the queue wiring is
commented rather than merely correct.
