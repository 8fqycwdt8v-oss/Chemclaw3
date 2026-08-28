# D-2026-08-27-one-lane-starts-the-fleet — a pidfile is a per-lane record of a machine-wide port

**Status:** accepted
**Context:** `infra/live/processes.sh` and `infra/live/e2e-full-stack/up.sh` both started `chem` and `safety`

## Context

Two live lanes exist. `infra/live/processes.sh` (`make live-up`) starts this repository's own
stack — connectors, four Temporal workers, the front door — and, since it needed them to boot at
all, the fleet's `chem` and `safety` servers on 8858/8859. `infra/live/e2e-full-stack/up.sh`
(`make live-e2e-full-stack`) starts the four-repo stack, and **calls `processes.sh up` as a
subprocess** after starting its own five fleet servers, two of which were `chem` and `safety`.

So this was never "two lanes a person might run together by accident". Every four-repo bring-up
started those two servers twice, because the second start is inside the first lane's own script.

**Reproduced, in both orders, on a running stack.** `start`'s guard reads `$RUN_DIR/<name>.pid`,
and the two lanes have different run dirs (`.live/run` and `.live/e2e/run`), so neither guard can
see the other's process. `wait_for` then polled a *URL*:

```
[e2e] chem started (pid 23894)
[e2e] chem ready                      <- answered by the other lane's server on 8858
...
.live/e2e-chem.log:
ERROR:    [Errno 98] error while attempting to bind on address ('127.0.0.1', 8858): address already in use
```

and the reverse order behaved identically:

```
[live] chem started (pid 24886)
[live] chem ready
[live] safety started (pid 24898)
[live] safety ready
```

with `.live/chem.log` carrying the same `Errno 98`. The result is a dead pidfile in whichever lane
lost the race, and `make live-e2e-full-stack-status` printing both halves of the contradiction in
one listing:

```
  chem             DOWN          <- e2e's own record
  safety           DOWN
  chem                 up   (pid 9177)     <- processes.sh's record, same server
  safety               up   (pid 9234)
```

while `curl 127.0.0.1:8858/healthz` returned 200 and `/readyz` was green throughout. Nothing was
broken; nothing could be trusted either. `processes.sh restart chem` in that state signals a pid
that no longer exists, and `down` reports a stop that did not happen.

The root cause is one sentence: **a pidfile is a per-lane record of a machine-wide resource.** The
port is the thing that collides, and no lane's bookkeeping can see it.

## The measurement

`docs/planning/BACKLOG.md` named two options and preferred moving the fleet bundles *out* of
`processes.sh`, on the grounds that the e2e lane supersedes the single-repo one — while asking for
the cost to be measured first. It was measured, and it does not survive the measurement.

With `chem` and `safety` unreachable, the front door does not come up degraded. It **exits 3**:

```
WARNING chemclaw.connectors.health: connectors unreachable at startup: chem (unreachable: ...), safety (...)
chemclaw.connectors.health.ConnectorsUnavailable: connectors_required is set but these connectors
are unreachable: chem (unreachable), safety (unreachable)
ERROR:    Application startup failed. Exiting.
```

That is `CHEMCLAW_CONNECTORS_REQUIRED=true` working exactly as intended and pinned deliberately
(LIVE-8: a configuration only production sets is a configuration nothing tests). So a `make live-up`
that did not start those two servers would not lose *some* coverage — it would lose the front door,
and with it every target that drives one:

| target | needs the front door | status if the fleet leaves `processes.sh` |
| --- | --- | --- |
| `make live-probes` | yes — 278 probes in `data/evals/probes/` | unrunnable |
| `make live-plan-gate`, `make live-degradation` | yes | unrunnable |
| `make live-storm`, `make live-soak` | yes | unrunnable |
| `make leak-probe` | yes | unrunnable |
| `make live-verifier-margin` | re-rolls the judge over probe transcripts | no transcripts to re-roll |
| `make live-jobs`, `make live-data` | no (Temporal + Postgres) | unaffected |

58 of the 278 probes name a `chem` or `safety` tool in `expects_tools` (`screen_hazards` alone in
32), which is the number one would guess at. It is the wrong number: the loss is 278, because the
dependency is a *boot* dependency, not a tool dependency.

The lane that cannot start without those two servers is `processes.sh`. That decides ownership.

## Decision

**`infra/live/processes.sh` is the only thing that starts `chem` and `safety`.** The four-repo lane
reaches them through the `processes.sh up` call it already makes, and no longer starts its own.

Four changes carry it:

1. **`up.sh` stops starting them.** `start_chem`/`start_safety` are deleted; `restart chem|safety`
   dies naming `processes.sh restart <name>` rather than reporting an unknown process; the README's
   process table and restart list say who starts what.
2. **`up.sh` keeps the *check*.** `assert_credential_accepted` runs against both servers after
   `processes.sh` returns. A check is not a start, and this one is that lane's own lesson
   (`D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two`): both
   halves of a connector token are set in two different places, `/healthz` is unauthenticated, and
   a mismatch is otherwise visible only as a degraded turn with nothing naming a credential.
3. **`up.sh` exports `CHEMCLAW_MCP_REPO`.** Both scripts read that variable and default it
   differently (`/workspace/...` there, `$REPO_ROOT/../chemclaw3-mcp` here). One owner only works
   from either lane's default if the resolved value is handed over.
4. **`wait_for` asks about its own process before it believes a URL** — in both scripts. This is the
   general fix and it is not limited to these two servers: a URL answering is evidence that
   *something* serves the address, never that this process does. Before and after, against a
   pidfile holding a dead pid while an unrelated server answers the same address:

   ```
   BEFORE  exit=0  [live] fake ready
   AFTER   exit=1  [live] fake exited before becoming ready — see .../fake.log
   ```

   Every process either lane waits on is now covered by that, including ones nobody has collided on
   yet.

`start_fleet_bundles` additionally asks the *address* before launching, when this lane has no live
pid for the server, and refuses with the cause rather than leaving the reader to find `Errno 98` at
the bottom of a log:

```
[live] chem: 127.0.0.1:8858 is already served, and not by a process this lane started.
This lane owns chem and safety; the four-repo lane reaches them by calling this script, so nothing
should be starting them twice. Stop the other server, or run `make live-e2e-full-stack`.
```

## Consequences

- `make live-up` alone is unchanged in what it exercises: 278 probes, the storm, the soak, the leak
  probe, the plan-gate and degradation suites. That was the whole point of taking the measurement.
- Running the fleet's own `make run-chem` (or a hand-started server on 8858/8859) beside
  `make live-up` now **fails the bring-up** instead of silently appearing to work. That is the
  intended direction: it used to "work" while leaving a lane whose status output was false.
- `up.sh restart chem` no longer restarts anything; it says where to. The chaos round loses nothing
  — `processes.sh restart chem` is the same primitive — but it is one more hop.
- The two lanes still keep separate run dirs, and that stays correct: they own disjoint process
  sets now, so there is nothing to share.
- **Adjacent and deliberately not fixed here:** `processes.sh restart <name>` calls `up`, which
  re-runs `connectors_dev --export-env` and *mints* new per-connector tokens unless they are already
  in the environment — leaving the still-running `connectors` process holding the old ones. It is
  pre-existing, it applies to every name the verb takes, and the workaround is the one the runbook
  already documents (`eval "$(bash infra/live/processes.sh env)"` first). Naming it here so the next
  reader does not discover it as a 401 from a healthy server.

## Alternatives considered

**Move the fleet bundles out of `processes.sh` and let the four-repo lane own them** — the option
`BACKLOG.md` preferred. Rejected on the measurement above: it does not reduce `make live-up` to a
smaller lane, it removes the front door from it, and with it the entire behaviour eval. The row's
premise — that the e2e lane supersedes the single-repo one — is true for *coverage* and false for
*cost*: the e2e lane needs three sibling checkouts, an npm install, a model credential and a
two-hour corpus backfill. `make live-up` is the lane anyone actually runs.

**Share one run dir, and teach one lane to adopt the other's processes.** Rejected. Adoption means
treating a server this lane did not start, and whose credential it did not set, as its own — which
is precisely the trust `assert_credential_accepted` exists because we do not extend. It also needs
the two scripts to agree on a directory they deliberately keep separate, and it leaves the question
"who restarts it" unanswered. One owner deletes the question instead of answering it.

**Give `start` a port argument and let it adopt any incumbent silently.** Rejected for the same
reason, plus one more: it would make the duplicate start *permanently* invisible rather than fixing
it, which is the failure mode this ADR exists to remove.
