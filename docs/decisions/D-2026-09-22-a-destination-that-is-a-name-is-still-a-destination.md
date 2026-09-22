# D-2026-09-22-a-destination-that-is-a-name-is-still-a-destination — deriving the git note remote

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Closes the first half of the
`BACKLOG.md` row *"The `git` remote is now a destination a deployment must declare, and nothing
derives it"*; its second half, the unmeasured IPv4-mapped arm, stays open.

## Context

`netguard.derive_allowed` builds the egress allowlist by reading the destinations off the settings
object — `llm_base_url`, `postgres_dsn`, `temporal_address`, each connector's URL. Its own docstring
says why: "reading them off the settings object rather than a static list is what keeps the
allowlist in step with the dial".

**One destination is not on that object.** `kg/git_writer.py` pushes notes with `git push
<git_remote> HEAD:refs/heads/<branch>`, and `git_remote` is the string `"origin"` — a *name*,
resolved inside the checkout, not an address. Neither the field's name nor its value contains a
host, so no walk over `Settings` finds it. `tests/test_netguard.py`'s derived guard over
destination-shaped fields anchors on the suffixes `_url|_endpoint|_address|_dsn`, and `git_remote`
ends in none of them.

That was harmless while nothing enforced the bound. It stopped being harmless when
`D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py` armed the compiled layer: a child
process inherits `LD_PRELOAD`, so `git push` is refused like any other dial. A deployment that
pushes notes off-box had to name its git host in `CHEMCLAW_EGRESS_ALLOW` by hand, and the symptom of
forgetting is a push refused by the deployment's own process.

## Decision

**Resolve it, in `derive_allowed`, and only when a deployment has moved `note_repo_dir` off its
default.** Three choices, each with a rejected alternative:

- **In `derive_allowed`, not in `cli/egress_preload.py`.** The entrypoint is where the subprocess
  would be cheapest — it already starts one interpreter to compute the allowlist for the compiled
  layer, which is what the backlog row proposed. But the two layers arm from *one* derivation: the
  compiled guard reads what `derive_allowed` returns and the in-process guard patches `socket` with
  the same set. A host added on the preload side alone would be a destination one layer permits and
  the other refuses, which is the defect this family keeps finding. Driven end to end: with a
  dedicated clone configured, `python -m chemclaw.cli.egress_preload` prints
  `enabled 127.0.0.1,localhost,notes.example.com`, and the entrypoint needed no edit at all.

- **Only off the default `"."`.** At the default the writer refuses the write before it can push —
  `git_writer._require_dedicated_checkout` will not commit into the running application's own tree
  and push to the source repository — so there is no destination to allow. Deriving one anyway
  would put the *source* repository's host on the allowlist of every dev checkout, a widening for a
  push that cannot happen. A single string comparison rather than importing that predicate, because
  `core/` may not import `kg/`; the alternative was moving the predicate into `core/`, which is a
  refactor across a layer boundary for one caller.

- **Absent beats wrong.** No git, no checkout, no such remote, a local-path remote, a wedged
  filesystem past the five-second bound — every one contributes nothing and none raises.
  `derive_allowed` runs at arm time in every process, so a raise there is a crashloop. An absent
  entry refuses a push a deployment can still permit through `egress_allow`; a wrong entry opens a
  host nobody declared.

## Consequences

**An allowlist entry now has a source that is a file on disk rather than a field on the settings
object, and that is the part worth arguing.** `.git/config` decides where the checkout pushes, so
writing it is equivalent to widening this entry. `git_writer._contained_note_path` already refuses
any note path reaching into `.git/` and says why in as many words — "the first turns a note write
into control over where this checkout pushes" — so the one agent-reachable path to that file was
closed before this ADR existed, for this exact reason, one layer down. What remains is the ordinary
assumption that a deployment's own clone is not attacker-writable, which is the same assumption its
`Settings` already rests on.

**A local-path remote is refused before the URL parser sees it.** `_host_from_url` reads `../notes`
as the host `..`, which would have put a nonsense entry on the allowlist; `file://` and an absolute
path have no host at all. Driven over nine spellings including the scp-like `git@host:path`, which
is the form that surprises — it has no scheme, so anything reading it as a URL sees no host unless
asked the right way.

**Revisit when:** a second destination arrives that is a *name* rather than an address — a registry
alias, a service name resolved from a file, an SSH host alias in `~/.ssh/config` (which would make
even a resolved git URL's host the wrong answer). At two, the pattern is worth extracting into
something the derived field guard can see; at one it is a special case with its own test. The file
that would show it is `tests/test_netguard.py::test_every_destination_field_is_derived_or_declared`,
which by construction cannot: it is the test that could not find this one either.
