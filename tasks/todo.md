# Wave 9 — destinations and bounds the guards cannot see

## Items

- [x] **Derive the git note remote, the one egress destination no field names** (`4b1a2c64`).
      `netguard.derive_allowed` reads every destination off a `Settings` field; `git_remote` is the
      string `"origin"` — a name, resolved inside the checkout — so neither the field's name nor its
      value contains a host, and the derived guard over `_url|_endpoint|_address|_dsn` cannot reach
      it. Harmless until the compiled `LD_PRELOAD` layer armed, after which `git push` is refused
      like any other dial. Resolved inside `derive_allowed` rather than in the entrypoint, so both
      layers keep arming from one set, and only once `note_repo_dir` has moved off `"."`. ADR +
      ledger + the row's first half deleted.
- [x] **Re-drive the `/readyz` row's trigger, which reads as fired and is not** (`4e331f6d`).
      psycopg 3.3.4 ships the deadline-respecting cancel the trigger named; the leg is still
      unbounded. Row rewritten with today's measurement, the near-miss that would have closed it
      wrongly, and a trigger that names the arm rather than the capability.
- [x] **Say it at the point of use too**: `_probe_database`'s docstring named `_try_cancel` without
      its new bound, so a reader checking upstream would have found the bound and concluded the
      docstring was stale.
- [ ] Full serial `make cov`, fresh-context subagent review, PR, merge on green CI.

## Measurements this wave rests on

- The git remote, driven against real `git remote add` over nine spellings: `https://`, the
  scp-like `git@host:path`, `ssh://user@host:port/path` and an uppercase host all resolve; a
  bare path, `file://`, `../notes` and `~/notes` resolve to nothing. `../notes` matters —
  `_host_from_url` reads it as the host `..`.
- End to end with a dedicated clone configured, `python -m chemclaw.cli.egress_preload` prints
  `enabled 127.0.0.1,localhost,notes.example.com`; on this checkout it prints neither.
- `/readyz` against `docker pause`d Postgres on psycopg 3.3.4: in-flight query on a held
  connection, >120 s against a 2 s budget; through `core/db.connection`, 2.00 s and a
  `TimeoutError`, 4 of 4 — a different leg, and the one that would confirm a fix that is not there.

## Review

Pending the gate and the subagent review.
