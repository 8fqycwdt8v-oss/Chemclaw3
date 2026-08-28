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
- [ ] `processes.sh` owns `calc`: export `CHEMCLAW_CALC_TOKEN`, start the backend on the port
      `settings.calc_server_url` names (the address the client actually dials), same
      already-served guard as `chem`/`safety`, and persist the token to `connector-env.sh`
- [ ] `up.sh` stops starting it: delete `start_calc`, keep `assert_credential_accepted calc`
      after `processes.sh` returns, route `restart calc` to `processes.sh` — the exact shape
      D-2026-08-27 gave `chem`/`safety`
- [ ] Docs: the four-repo README process/restart tables, the runbook's live-up line
- [ ] New ADR superseding the "unaffected" row + ledger row
- [ ] Verify: `make live-down`, then the runbook's sequence verbatim in a clean shell -> 5/5
- [ ] `make lint type test` green

## Review

(filled in at the end)
