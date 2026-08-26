# Backlog / DEFERRED sweep — 2026-08-26

## Task
Work the queue rather than add to it: pick the rows in `docs/planning/BACKLOG.md` that are
closable **offline and in full**, re-measure each against `HEAD` first (the file's own rule),
implement, prove, and delete the row in the commit that closes it.

`DEFERRED.md` was read end to end: **no row's trigger has fired.** Every one is gated on a
cluster, a tenant, a licence, an upstream release or a corpus none of which this environment
has. Nothing to close there — which is itself the answer to "what should be done" for that file.

## Selected rows (7)

Rejected as not-now, with the reason, so the next session does not re-derive it:
`CalculationKey` escaping (needs an ADR + a full-cache-invalidation migration plan),
`fetch_artifact` (waits on the fleet's `vibspectrum`), tool-result framing (ADR-sized, needs a
content-field convention), the pydantic-repr row (blast radius is every tool at once),
`observations_status_idx` and `session_owners` disposal (both need a product call, not a diff),
the whole of §5 (blocked on a working model credential or on deployment history).

- [x] **1 · §1 The audit trail's `agent` column can never be non-empty.** `set_current_specialist`
      has zero callers in `src/`; `record_handoff` has none anywhere. Delete the contextvar trio,
      `record_handoff`, `HandoffSignal` and the audit write. Keep `HandoffEvent` (a union member is
      a three-repo change) and the SQL column (a merged migration is never edited). ADR.
- [x] **2 · §2 `changes_between` diffs against *absent*.** Move `_changes`'s "both sides recorded
      it" rule out of `agent/condense.py` and into `memory/progression.py`, one rule instead of two.
- [x] **3 · §3 A rejoined durable run never reaches the second chemist.** `handle.describe()` on
      the `WorkflowAlreadyStartedError` path; announce when the status is RUNNING.
- [x] **4 · §4 `connectors.<name>.enabled` never reaches the agent.** A `chemclaw.connectorsEnabled`
      helper mirroring `connectorUrls`; delete the sentence pointing at the absent key.
- [x] **5 · §4 One `replicas` knob drives two differently-shaped Deployments.** Split into
      `serverReplicas`/`workerReplicas` defaulting to `replicas`; fix the `nil | int` = 0 hole a
      `url:` bundle with a worker leaves in the connection ceiling.
- [x] **6 · §4 Egress is still port-scoped by default.** Empty `egressDestinations` under an
      enabled policy must `fail`, with an explicit `allowAnyDestination: true` escape hatch.
- [x] **7 · §4 Three credentials are plain `str`.** `SecretStr` on `llm_api_key`, `hpc_api_token`,
      `temporal_api_key` — defence in depth beside the redacting filter, which stays.

## Verification
`make lint type test` with the Postgres/Temporal stack **up** (`dockerd` + `make up` +
`make db-migrate`) — a green run that skipped ~157 Postgres tests proves nothing about the
durable layer. Report what was skipped.

## Review

**Three ADRs**, one per decision rather than one per commit:
`an-attribution-nothing-can-write-is-not-an-attribution` (row 1),
`a-knob-that-renders-nothing-is-not-a-knob` (rows 4–6, one failure with three faces),
`a-credential-is-a-type-not-a-convention` (row 7). Rows 2 and 3 are defect fixes with tests and
need no decision recorded.

**Two rows were wrong as written, and correcting them was part of the work** — which is what the
file's own header asks for.

1. **Row 2 asked for too much.** `BACKLOG.md` said the "both sides recorded it" rule should cover
   the two setpoints *and* the species sets. Applied to species, `test_a_reagent_added_mid_procedure_is_diffed_too`
   went red — correctly. A setpoint is an optional scalar, so `None` means nobody wrote it down; a
   role's species set is derived from a components list that is *present either way*, so an empty
   `reagent` set is the record saying the run used no reagent. That is a real change and the most
   common one a series carries. The rule now covers optional scalars and stops there, with the
   asymmetry pinned by a test so nobody "unifies" it later.
2. **Row 7 was three fields and is seven.** A settings object where some secrets hide in a `repr`
   and others do not teaches the wrong rule. Two things surfaced on the way: `llm_fallback_api_key`
   was in no redaction list at all — the one credential nothing covered — and both readers in
   `core/logging.py` test `isinstance(value, str)`, which a `SecretStr` is not, so the "hardening"
   would have silently switched the redaction off for exactly the fields it hardened.

**One new row queued**, from the same measurement: `hpc_artifact_store_token`,
`llm_fallback_api_key` and `temporal_api_key` are typed and read and have no chart Secret key, so a
deployment cannot set them at all. Typing them did not fix that and the row says so.

**Nothing was closable in `DEFERRED.md`.** Every row is gated on a cluster, a tenant, a licence, an
upstream release or a corpus this environment does not have. That is the answer to "what should be
done" there, not an omission.

**What the fail-closed chart costs.** `helm template` on the shipped defaults now needs
`--set networkPolicy.allowAnyDestination=true`. Three call sites pay it and a test asserts every
shipped-defaults render carries it, so the next one added without it fails offline. `helm` is not
installed here, so the render itself is unproven until `make helm-validate` runs on a machine that
has it — every assertion added is over template *text*, like the rest of that suite.
