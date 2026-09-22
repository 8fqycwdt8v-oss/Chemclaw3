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
- [x] **Fresh-context subagent review.** Four defects in the derivation, all fixed in `6901d62f`,
      each now a test that fails without the fix: `get-url` returned the *fetch* URL where `git
      push` uses `pushurl`; `_host_from_url` outside the `try` made one legal `.git/config` an
      import-time crash in every process; the `"."` guard was a string comparison that `./`,
      `src/..`, `$PWD`, an absolute path and a symlink all walked past; and a comma in a derived
      host is two allowed hosts on the compiled layer and none on the Python one.
- [x] **Full serial `make cov`**: **10,609 passed, 8 skipped**, coverage **89.93%** against a floor
      of 84.0, 34m25s, with Postgres and Temporal up.
- [ ] PR, and merge when CI is green.

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

**The review found more than the wave did.** Two of its four findings — the `pushurl` case and
the crashloop — make the shipped guard do the wrong thing, and neither was reachable from the tests
written with the change: they drove the *forms a URL can take* and never the *ways git can be
configured*, nor the ways `urlsplit` can refuse one. The third and fourth are the same shape one
level out: a predicate spelled twice (`repo_dir == "."` here, `Path.resolve()` there) drifted
apart, and a value crossing a layer boundary re-created the divergence the design was placed to
prevent — through the data rather than through the code.

What generalises: **an ADR that says "none raises" is a claim a test should hold**, and this one
said it while the code could crashloop every component. The fix is not more prose. `core/checkout.py`
exists so the two spellings cannot drift, `test_the_derivation_and_the_writers_refusal_ask_the_same_question`
holds them together, and five mutations of the new code are each caught by a named test.

The psycopg finding needed no fix and was the wave's other half: a trigger that reads as met, and
the near-miss measurement that would have closed the row wrongly.
