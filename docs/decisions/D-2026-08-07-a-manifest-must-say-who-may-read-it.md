# D-2026-08-07-a-manifest-must-say-who-may-read-it — the mount is a boundary, and omission is not a decision

**Status:** accepted

## Context

The third family from the share review, after data loss
(`D-2026-08-07-the-mark-means-observed-not-processed`) and availability
(`D-2026-08-07-one-bad-file-must-not-stop-the-corpus`). These are security and correctness: nothing
here loses a document or stops a job, and each one either hands out something it should not or
answers with something that is not true.

**The entitlement was opt-in.** `required_roles` defaulted to `[]`, and `_entitled()` returns True
for an empty set. The documented way to attach a real share is to hand-author a `datasource.yaml`
and put its folder first on `CHEMCLAW_DATA_SOURCES_DIR` — so a binding that named `mount` and
`roots` and simply forgot `required_roles` served the whole AD-gated drive to every authenticated
user, with no warning, no log line, and nothing to distinguish it from a correctly gated one. The
retrieve half's own docstring calls this "the whole security model".

**The mount was not a boundary.** `descend`'s symlink guard runs per *entry*, so it never sees the
root directory it was handed. `crawl_share` passed `walk.mount / root.path` straight to `scandir`:
`is_dir()` follows a link, and the entries under it are ordinary files whose `is_symlink()` is
False, so nothing fires. `Projects -> /` would index the container filesystem — the Git-backed
knowledge repo included — as cited evidence, under paths that still look mount-relative in the
cursor, the citation and the logs. `follow_symlinks: false` does not help; it only skips symlink
*entries*. `RootBinding._stays_inside_the_mount` rejects `..` and absolute strings, which is a
lexical check against a filesystem-level escape.

**Group claims widened every unrelated gate.** `api/auth.py` merged `roles` and `groups` into one
flat set, and that same set is read by `entra_privileged_roles`, `tool_role_gates` and
`skill_access`. The comment asserted the values are group object-ids — but that is a *tenant*
setting, not a guarantee: `groupMembershipClaims` can emit `sam_account_name` or `cloud_displayname`
instead. Turning the flag on to give one file share its read entitlement could therefore let anyone
in a directory group named `process-chemist` pass every write-tool gate.

**The read trusted a stat from another activity.** `_read_and_parse` re-opened `ref.absolute` with
`Path.read_bytes` — no `O_NOFOLLOW`, no size re-check — minutes after the crawl checked it, on a
share every member can write to. Two exploits from one primitive: publish a file, wait to be
accepted, then swap it for a symlink to the workload-identity token; or grow a 1 KB `.csv` to 20 GB
before the read.

And two correctness defects, both measured:

- `_cosine` exceeds 1.0 for **996 of 2000** normalised vectors (two square roots in the denominator,
  worst `1.0000000000000002`), and `DocumentHit.score` is bounded `le=1.0` — so an exact match
  raised `ValidationError` from inside the reference implementation every test validates against.
- `_LABEL` matched any bracketed line under 80 characters. A paragraph opening
  `[Figure 2: yield vs time]` became the chunk's citation coordinate **and was stripped from the
  body**, so the citation named a location the chunk did not come from and the caption stopped
  being searchable. The comment claimed prose "cannot be mistaken for one"; length is not a
  vocabulary.

## Decision

### A manifest must say who may read it

`required_roles` or `public: true` — **one is mandatory**, and setting both is refused. Omitting
both is a load-time error naming both choices, so `make datasource-validate` catches it rather than
a chemist discovering it in an answer.

`public` exists so that "ungated" is something a manifest *says* rather than something it omits. An
author who means it writes one word; an author who forgot gets an error. This is breaking for any
binding that omitted the field — in this repository, none.

### The root is resolved, like every entry under it

`_within_mount` moves to module scope (both call sites need it) and `crawl_share` asks it of the
root directory before descending. An escaping root joins `failed_roots`, so it is reported *and*
the sweep is suppressed — an escape and an empty root must not look alike.

### Group-derived entitlements are namespaced

`GROUP_ROLE_PREFIX = "group:"`. Two namespaces, kept apart: app roles are values the API's own
registration defines, group claims are values the directory defines, and a tenant may render the
latter as names. A group-gated share's `required_roles` holds `group:<object-id>`, which
`docs/guides/sharedrive-concept.md` now documents.

### The open re-checks rather than trusts

`os.open(..., O_RDONLY | O_NOFOLLOW)` and the size re-read from the *open descriptor*. The check and
the read are the same operation, or the swap moves into the gap between them.

### Two correctness fixes

`_cosine` clamps to [0, 1] exactly as the Postgres backend does — the two backends now agree at the
boundary rather than one raising where the other clips. `_LABEL` is anchored to `page|slide|sheet`,
the three words the parsers actually emit: a document cannot forge a coordinate it was never given.

### `**/Archive/**` excludes a top-level `Archive`

`fnmatch` gives `**` no special meaning — it translates to `.*?/Archive/`, which requires a
separator *before* `Archive`, so the shipped pattern did not match `Archive/old.pdf` at all. Each
pattern is now tried against the path, the path with a leading `/`, and the basename. Strictly more
permissive, so no existing exclusion changes meaning.

## Consequences

- Eleven new tests. The suite in this file goes from 38 to 49.
- **Breaking:** every `datasource.yaml` carrying a share must now declare `required_roles` or
  `public`. The failure is loud and at load.
- Exclusion is still **case-sensitive**, which mismatches CIFS (`Archive`/`ARCHIVE`/`archive` are
  one folder to the file server). Filed in `docs/planning/BACKLOG.md` rather than changed here:
  case-folding every pattern would quietly widen exclusions a deployment already relies on.

## Alternatives rejected

**Default `required_roles` to deny-all instead of requiring the field.** Fails safe, and
non-breaking. Rejected because "the share indexes fine and returns nothing to anybody" is a
confusing failure that a deployment debugs by loosening the gate — the opposite of what it should
learn. An error that names both choices teaches the right thing once.

**Warn loudly on an ungated share instead of refusing.** Relies on someone reading worker logs at
startup, which is exactly how the defect went unnoticed. A warning is what you write when you cannot
refuse; here refusing costs one line in a manifest.

**Resolve every path under a root, not just the root.** `descend` already does that per entry when
following links. Resolving unconditionally would `stat` twice per file on a 500k-file walk whose
whole design is one `scandir` pass.
