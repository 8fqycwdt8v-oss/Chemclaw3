# Task: `make live-up && make live-jobs` cannot run — the calc backend has no owner

## The defect (measured, not argued)

`make live-jobs` is the durable half of the live lane. `cli/live_jobs.py` says in its own
docstring that it exists to be runnable "where no model credential exists — which is most CI
runners, and this repository's own agent containers", and `docs/guides/runbook.md` calls Stage A
"the load-bearing one". Run as documented it fails at the first job:

    make live-infra && make db-migrate && make live-up && make live-jobs
    -> ConnectorJobError: the 'compute_reaction_energy' job ran and failed:
       CalcServerError: the calculation service is not answering

`processes.sh` starts `chem` and `safety` from the fleet checkout and `worker-calc`, whose
activities dial the `calc` **backend** at `settings.calc_server_url` (8860) on a cache miss. No
lane starts that server for `make live-up`; only the four-repo lane does. `calc` is not a
connector, so `check_connectors_at_startup` never probes it and `/readyz` is green with it down.

Starting it by hand gets a second, distinct failure — `processes.sh` exports dev tokens for
`chem` and `safety` and none for `calc`, so the worker sends a bearer the server refuses:

    CalcToolError: the calculation service refused this client's credential (HTTP 401)

With both supplied by hand: **5/5 checks passed**. So the spine is sound; only the lane is.

## Why this is D-2026-08-27's rule, not a new one

That ADR decides ownership by one test — *the lane that cannot start without a server owns it* —
and applies it to `chem`/`safety` via the front door's boot dependency. Its consequences table
lists `make live-jobs` as **"unaffected"**. That line is the error: the durable half has a
run-time dependency on `calc` that nobody asked about, because the analysis was framed entirely
around what blocks the front door from booting.

## Plan

- [x] Reproduce both failures and prove 5/5 with the server and token supplied by hand
- [x] `processes.sh` owns `calc`: export `CHEMCLAW_CALC_TOKEN`, start the backend on the port
      `settings.calc_server_url` names (the address the client actually dials), same
      already-served guard as `chem`/`safety`, and persist the token to `connector-env.sh`
- [x] `up.sh` stops starting it: delete `start_calc`, keep `assert_credential_accepted calc`
      after `processes.sh` returns, route `restart calc` to `processes.sh` — the exact shape
      D-2026-08-27 gave `chem`/`safety`
- [x] Docs: the four-repo README process/restart tables, the runbook's live-up line
- [x] New ADR superseding the "unaffected" row + ledger row
- [x] Verify: `make live-down`, then the runbook's sequence verbatim in a clean shell -> 5/5
- [x] `make lint type test` green

## Review

**Done, and measured at every step rather than argued.**

`make live-up && make live-jobs`, from a torn-down lane and a cleared port with
`CHEMCLAW_CALC_TOKEN` unset in the environment: **0/5 -> 5/5**. The lane now logs `calc started` /
`calc ready` itself, and `processes.sh status` lists it beside `chem` and `safety`.

Gate: `make lint` clean, `mypy --strict` clean across 734 files, `make test` **5753 passed, 14
skipped** in 12m36s against real Postgres — identical to the pre-change baseline taken this
session, so nothing regressed. The 14 skips are all named by the run: 9 need `helm`, 3 need
untruncated git history, 2 attempted a real Anthropic call (the environment's `API-KEY` is present
but the provider refuses it for want of credit).

**What made this hard to see, and is worth carrying forward.** Every signal the lane offers said it
was healthy — `/readyz` green, `make live-status` complete, no error at bring-up — because the one
missing server is deliberately not a connector and so is outside everything that probes. The
failure only exists at job time. D-2026-08-27 reasoned carefully about ownership and still got the
`live-jobs` row wrong, because it asked what blocks the *front door* from booting and never asked
what the durable half needs while running. Two dependency questions, one of which nobody had a
habit of asking.

**Not fixed, deliberately:** the backend has no startup readiness probe, so a misconfigured
`calc_server_url` is still discovered at job time rather than at bring-up. Argued in the ADR's
alternatives — the cache means a turn may never dial it, and a probe would make an optional
dependency mandatory for every process importing the bundle. `CalcServerError` already names the
cause precisely when it does happen.
