# D-2026-08-28-the-durable-half-has-a-backend-too — the lane that owns a server is the one whose work needs it

**Status:** accepted
**Context:** `make live-up && make live-jobs` — the documented, model-free half of the live lane — could not pass a single check

## Context

`D-2026-08-27-one-lane-starts-the-fleet` settled which lane starts a fleet server, and it settled it
with one test: **the lane that cannot start without a server is the lane that owns it.** It asked
that question of the front door, found `chem` and `safety` to be boot dependencies under
`connectors_required=true`, and gave both to `infra/live/processes.sh`.

Its consequences table lists the two targets that do not need the front door:

| target | needs the front door | status if the fleet leaves `processes.sh` |
| --- | --- | --- |
| `make live-jobs`, `make live-data` | no (Temporal + Postgres) | unaffected |

That row is the error this ADR corrects. `make live-jobs` was not unaffected; it had never worked
on this lane at all.

## The measurement

Run exactly as `docs/guides/runbook.md` prescribes — the sequence it introduces with "**Stage A
(`make live-jobs`) needs no model credential and is the load-bearing one**":

```
make live-infra && make db-migrate && make live-up && make live-jobs

ConnectorJobError: the 'compute_reaction_energy' job ran and failed:
CalcServerError: the calculation service is not answering, so no calculation was run.
```

`processes.sh` starts `worker-calc`, whose activities reach the physics through
`science/calc/store.py::cached_compute` → `connectors/calc/remote.py::calc_session`, which dials
`settings.calc_server_url` (8860) on a cache miss. Nothing in this lane started that server. Only
the four-repo lane did.

**Nothing could have caught it.** `calc` is not a connector — deliberately, and its manifest says so
in a box — so `check_connectors_at_startup` never probes it, `/readyz` is entirely green with it
down, and `make live-status` listed a complete stack. The lane reported itself healthy and the first
durable job failed.

Starting the server by hand produced a second, independent failure:

```
CalcToolError: the calculation service refused this client's credential (HTTP 401 from
http://127.0.0.1:8860/mcp). The service is running and answering; it does not accept the bearer
taken from CHEMCLAW_CALC_TOKEN.
```

`start_fleet_bundles` exports a dev token for `chem` and for `safety`; nothing exported one for
`calc`, so the worker sent a bearer the server does not verify. This is precisely the blind spot
`D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two` named, and
`assert_credential_accepted` exists to close — in the *other* lane.

With the server started and the token exported by hand: **5/5 checks passed.** So the durable spine
was sound the whole time, and only the lane around it was not.

## Decision

**`infra/live/processes.sh` starts the `calc` backend, on the same terms it starts `chem` and
`safety`.** This is D-2026-08-27's rule applied to the question it did not ask: the durable half's
work is what `calc` is a run-time dependency of, so the durable half's lane owns it.

1. **`processes.sh` starts it** (`start_calc_backend`), with the same already-served address guard,
   and exports `CHEMCLAW_CALC_TOKEN` beside the other two — both halves of the credential, one
   variable name, as the fleet requires.
2. **The port comes from `settings.calc_server_url`, not from a manifest.** That is the address the
   *client* dials, so any other source can drift from it, and a drift here surfaces as a connection
   refused rather than as a configuration error. `manifests-internal/calc/` is also, by design,
   somewhere `fleet_port` cannot read.
3. **The token is persisted to `connector-env.sh`** with the other two, so the runbook's
   `eval "$(bash infra/live/processes.sh env)"` gives a second shell a bearer the server accepts.
4. **`up.sh` stops starting it and keeps the check** — `start_calc` is deleted,
   `assert_credential_accepted calc` runs after `processes.sh` returns, and `restart calc` dies
   naming `processes.sh restart calc`. Exactly the shape D-2026-08-27 gave `chem` and `safety`.

`down`, `status` and `restart` needed no change in either script: all three iterate pidfiles, so a
server the lane starts is a server the lane already accounts for.

## Consequences

- `make live-up && make live-jobs`, with nothing set by hand, is **5/5**. Verified from a torn-down
  lane and a clear port, with `CHEMCLAW_CALC_TOKEN` explicitly unset in the environment.
- `make live-up` now requires the `Chemclaw3-mcp` checkout for one more reason. It already required
  it — `chem` and `safety` are a boot dependency and the script already died without it — so no lane
  that worked before stops working. The `die` message names the calc backend now too.
- Running the fleet's own `make run-calc` beside `make live-up` fails the bring-up with the cause
  instead of an `Errno 98` at the bottom of a log, which is the direction D-2026-08-27 chose.
- **`make live-data` is the other row in that table, and it is genuinely unaffected**: it reads
  Postgres and the corpus, and calls no calculator. The row was half right, which is why nobody
  looked at the other half.

## Alternatives considered

**Leave ownership with the four-repo lane and have `make live-jobs` say it needs it.** Rejected. The
four-repo lane needs three sibling checkouts, an npm install, a model credential and a two-hour
corpus backfill; `live_jobs.py`'s own docstring says it exists to be runnable "where no model
credential exists — which is most CI runners, and this repository's own agent containers". Making
the model-free target depend on the lane that needs a model inverts the reason it was built.

**Have the calc client fail at startup instead of at job time.** Rejected here as out of scope, and
not obviously right: `calc_server_url` is a *backend* address, the cache means a turn may never dial
it, and a startup probe would make an optional dependency mandatory for every process that imports
the bundle. The honest fix for the lane is to start the server; a readiness probe for the backend is
a separate question, and `CalcServerError` already names the cause precisely when it happens.
