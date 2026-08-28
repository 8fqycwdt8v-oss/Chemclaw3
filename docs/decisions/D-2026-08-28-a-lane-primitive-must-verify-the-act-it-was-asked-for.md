# D-2026-08-28-a-lane-primitive-must-verify-the-act-it-was-asked-for — four defects, one shape

## Status

Accepted, 2026-08-28.

## Context

Bringing the live lane up in a posture it had never been run in — three repositories, a scripted
model, no credential — surfaced four defects in one afternoon. Each was found by the next one being
unreachable until the previous was fixed, so they are recorded together: individually they look like
four unrelated oversights, and they are one.

| # | The defect | How it presented |
|---|---|---|
| 1 | `restart-postgres` had no compose branch | Logged "postgres up"; `pg_postmaster_start_time()` unchanged. The storm's E3 check scored PASS over a bounce that never happened |
| 2 | The lane started five of six published fleet manifests | The front door refused to boot: `connectors_required is set but these connectors are unreachable: pyexec` |
| 3 | `processes.sh env` carried credentials but not the fleet checkout | Every chaos restart from a second shell died: "chem and safety are served by Chemclaw3-mcp, which is not at …/../chemclaw3-mcp" |
| 4 | `env` also carried no model posture, and `restart` did not verify | `api killed`, then `skipping the front door`, then **`live stack up`**, then exit **0** — with the front door gone |

## The shape

**Every one of these was a lesson learned once and written too narrowly.**

- An earlier pass found #1's exact defect for `stop-temporal`/`start-temporal` and fixed it by adding
  `compose_temporal_id()` — a fix to *Temporal*, not to the class. The second copy sat in the same
  file, unlooked-for.
- `D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two` fixed #2's
  count of the day by naming the missing servers. The fleet then grew `pyexec` and the list went
  stale exactly as a hand-kept list does.
- #3 and #4 are the same omission one variable apart: `env` is documented in its own comments as
  "the contract" a second shell reads, and it carried the values that were *minted* while omitting
  the values that were *resolved* — a checkout path, a provider, a base URL, a model name.

So the fixes are class fixes rather than instance fixes: `compose_service_id <name>` replaces
`compose_temporal_id`; the fleet start list is derived from the manifests actually mounted rather
than written down; and the env contract carries every value this invocation settled that nobody
downstream can re-derive.

## Decision

**A lane primitive must verify the act it was asked for, and a check that disturbs something must
verify the disturbance independently.** Two halves, deliberately both.

- `restart <name>` ends by asserting `<name>` is running, and fails otherwise. `up` is right to skip
  a front door with no model configured — it is being asked to bring up whatever the configuration
  describes. `restart api` is not, because it was asked about one named process. A reason, however
  good, is not an outcome.
- `_chaos_postgres_bounce` reads the postmaster's own start time either side and fails when it did
  not move. Fixing a primitive is no reason to keep trusting the check that could not see it break;
  the next primitive to silently no-op will be a different one.

The two are not redundant. The actor verifies the act; the observer verifies it again, from
outside. Defect #4 is what a lane looks like when neither does: a killed process, a printed
"live stack up", and an exit code of 0.

## Consequences

- Every E3 result recorded before today is not evidence about pool recovery on a Docker lane.
- Any storm run driven from a second shell before today either died at its first restart (#3) or
  measured turns against a front door it had itself removed (#4). The 2026-08 storm report's family
  A and E rows should be read with that in mind.
- `tasks/lessons.md` carries the general rule, which is not about shell scripts: when a fix is
  "this function forgot the other branch", the next question is always *which other functions have
  the same shape*, and the answer belongs in the same commit.
