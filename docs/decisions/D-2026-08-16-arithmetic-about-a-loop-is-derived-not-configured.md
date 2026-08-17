# D-2026-08-16-arithmetic-about-a-loop-is-derived-not-configured — two HPC timing relations become properties, and one startup refusal is retired

**Status:** accepted · **Date:** 2026-08-16 · Narrows `Settings._poll_faster_than_heartbeat`
(F5/D-048) and adds one required value to `_hpc_launch_config`.

## Context

The QM/HPC path was driven against the companion mock launcher (`Chemclaw3_mock/app/hpc`) under
`temporalio.testing.ActivityEnvironment`. Three of its settings were mis-paired at the **shipped
defaults**, and each mis-pairing was accepted at startup:

- `qm_activity_timeout_seconds` (30 s) bounds `submit_to_hpc`; `hpc_http_timeout_seconds` (30 s)
  bounds the launch POST *inside* it. Equal, so a slow launcher races its own start-to-close.
  Measured by cancelling a 3 s launch at the activity boundary: attempt 1 lost the run id, attempt 2
  launched again, and the launcher accepted **two** runs — one of which nothing will ever poll,
  cancel or bill to a job.
- `_poll_faster_than_heartbeat` compares `hpc_poll_interval_seconds` against the heartbeat timeout.
  But `_poll_nextflow` beats once at the top of each loop and then makes an HTTP call *before*
  sleeping, so the real gap between beats is `hpc_http_timeout_seconds + hpc_poll_interval_seconds`.
  Measured: `hpc_http_timeout_seconds=300` against the shipped 120 s heartbeat timeout was accepted,
  leaving 302 s between beats — Temporal declares a healthy worker dead, retries the poll elsewhere
  while the first is still polling, and burns the attempt budget on a run that is fine.
- `hpc_api_token` was excluded from the `nextflow` required set. `_auth_headers()` returns `{}` for
  an empty token rather than refusing, so a secret that failed to mount produced green pods, green
  probes, and a first DFT job dying five retried attempts deep on `launch failed: 401` — verbatim
  the outcome that validator's own docstring says it exists to prevent.

The obvious fix for the first two is a validator per relation. It was written, and it **refused the
shipped chart**: `2 * 30 >= 30`, so every `nextflow` deployment would have failed to boot until an
operator tuned a pair of unrelated knobs.

## Decision

**A relation the code already knows is derived, not configured.** The first two become properties:

- `hpc_submit_timeout_seconds` = `max(qm_activity_timeout_seconds, 2 * hpc_http_timeout_seconds + 5)`,
  used as the launch activity's `start_to_close`. Doubled so the POST can time out *and* the
  activity still has room to report that it did.
- `hpc_effective_heartbeat_timeout_seconds` = the configured value, **floored** by
  `hpc_http_timeout_seconds + 2 * hpc_poll_interval_seconds`.

The third is a genuinely missing *value* rather than a relation, so it becomes what it should always
have been: `hpc_api_token` joins `_hpc_launch_config`'s required tuple and a misconfigured
deployment refuses to start.

**The nextflow half of `_poll_faster_than_heartbeat` is retired.** The floor makes it unreachable —
the derived heartbeat timeout always exceeds one interval — so keeping it would only refuse
configurations that are now safe. The mock-path check stays: that path's heartbeat is not floored.

## Why derive rather than validate

A validator and a derivation encode the same relation; they differ in who is made responsible for
it. The test is whether the relation is **a policy an operator holds an opinion about** or
**arithmetic about a loop we wrote**.

"How fast should a genuinely dead worker be noticed" is a policy — so `hpc_run_heartbeat_timeout_seconds`
stays a setting and still wins whenever it is above the floor. "The heartbeat timeout must exceed
one HTTP round trip plus one sleep, because that is what this loop does between beats" is not a
policy; it is a fact about `_poll_nextflow` that no operator should have to re-derive, and that a
validator would merely force them to state back to us. Raising `hpc_http_timeout_seconds` for a
sluggish Tower now needs no second edit.

The cost is that a derived value can exceed what an operator wrote, which is why both properties are
named, documented with the measurement, and floored rather than replaced — the configured value is
still visible in `describe` and still authoritative in the normal case.

## What this was verified against

- Both mis-pairings reproduced against the mock launcher before the change; both closed after.
- The refused-shipped-chart outcome is why the validator was reverted — it is recorded here rather
  than deleted silently, because the next person to see these two knobs will reach for it again.
- `tests/test_config.py` pins the floor, the doubling, and the credential refusal.
- `make lint type test` green with a live Postgres and Temporal.
