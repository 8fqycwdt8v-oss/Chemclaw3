# D-2026-08-09-a-hand-written-list-of-columns-drifts — seven review findings against the offboarding and seam work

**Status:** accepted

## Context

A deep review of D-2026-08-08's three changes — the data-source naming fix, the connector-declared
knowledge vocabulary, and the new offboarding command — found seven defects. All seven were
reproduced before being fixed. Two are worth recording as decisions rather than as patches, because
each is a *shape* that will recur.

## The two that are decisions

**1. A hand-written list of columns drifts, so derive it.** `_RETAINED` enumerated six actor-bearing
columns from memory and missed two: `note_proposals.decided_by` and `bo_campaigns.opened_by`. The
consequence was specific and bad — a departing *PR-gate reviewer* was told that zero
`note_proposals` rows mentioned them, while the column recording every sign-off they had ever given
still held their oid. The command's whole claim is that it reports both tiers honestly; a missing
column made it under-report exactly the tier it refuses to delete.

The fix is not "add the two columns". It is
`test_every_actor_bearing_column_in_the_schema_is_accounted_for`, which reads
`information_schema.columns`, filters to the six spellings this system uses for a person
(`actor`, `owner`, `holder`, `requested_by`, `decided_by`, `opened_by`) and asserts every one
appears in the erase tier or the retain tier. A migration that adds a seventh now fails a test that
names the column, and the author has to place it. **Deciding by omission is the failure; the test
removes the option.**

This also forced `_RETAINED` to carry *columns*, plural — `note_proposals` names a person twice, so
a table-to-column mapping could only ever report one of them. One row counts once however many of
its columns match: a proposal somebody both wrote and reviewed is one retained record.

**2. A validator that passes what startup refuses is worse than no validator.** A `datasource.yaml`
with `name` in its `config:` block passed `make datasource-validate` and then died at startup with
`got multiple values for keyword argument 'name'`. The validator built the kwargs as a *dict*, where
the second `name` silently overwrote the first; the registry splats them, where it is a duplicate
keyword. Two representations of "the arguments this half receives", agreeing until they didn't.

`DataSourceManifest` now rejects a `config: name:` outright — a source's name is its folder, and the
retrieve half is handed it automatically, so the key can only ever be a mistake. Refusing it at the
manifest means both the validator and the registry get their answer from the same place. This is the
same class of defect the audit that started this work found in `datasource-validate` (it passed a
two-share configuration that lost data), and the same lesson: the validator is what an operator
trusts *before* deploying, so a false green there is more expensive than no check.

## The five that are ordinary fixes

- **`make user-erase APPLY=0` committed the erasure.** `$(if $(APPLY),…)` is a non-empty test, so
  `0` and `false` both read as true — on the one irreversible target in the Makefile, where "I
  explicitly said no" must not delete anything. Now compared to the literal `1`, with an
  unrecognised value reported and treated as a dry run.
- **`kg-validate` died with a traceback about connectors.** Resolving the effective vocabulary asks
  the connector registry, so a `CHEMCLAW_CONNECTORS_ENABLED` naming a bundle the image does not ship
  crashed the *graph* gate. Still a failure — it is a real misconfiguration — but reported, like
  every sibling validator reports its configuration errors.
- **A refused statement escaped the erase CLI as a traceback.** `psycopg.Error` is neither a
  `ConnectionError` nor a `ValueError`, so `InsufficientPrivilege` — what a deployment gets when
  `make db-grants` has not been re-applied for this command's own `DELETE ON session_owners`, the
  likeliest failure the first operator will hit — was uncaught. Translated at the seam into
  `ErasureError` rather than caught in the CLI, because `tests/test_third_party_layering.py`
  forbids `chemclaw.cli` from importing a database driver at all, and it is right to: a terminal
  entry point should not know which driver is underneath. `ErasureError` is registered
  non-retryable in `durable/publish.py` like every other `ChemclawError`.
- **A turn lease outlived its holder.** The erase tier reached `session_turns` only through
  `session_owners`, so a lease held on a session the leaver did not own stayed. Now matched on
  `holder` as well.
- **Two tests proved nothing.** `_seed` never inserted a `subscriptions` row, so the watch deletion
  ran against zero rows in every test while the docstrings claimed coverage — a statement executed
  with nothing to delete proves only that it parses. And
  `assert issubclass(psycopg.OperationalError, Exception)` is true of every exception in Python; it
  would have passed with the CLI's error handling deleted. Its replacement drove the real entry
  point at an unreachable port — and *still* passed against the narrow `except`, because `core.db`
  already translates that into `ConnectionError`. Only the third version, pointing the search path
  at an empty schema to force a statement-level error against a healthy database, discriminates.

## Consequences

- Three guards in this repository caught defects in this change that the review did not have to:
  `test_database_privileges` (a missing grant), `test_third_party_layering` (the driver import in
  `cli`), and `test_publish` (an unregistered error class). Each fired the moment the code changed.
  That is the argument for writing the schema-derived test above rather than a fourth list.
- Every fix here has a counterfactual: reverting it makes a named test fail. The two that did not
  discriminate on the first attempt were rewritten until they did, and the false starts are recorded
  in the test docstrings so the next author does not repeat them.
