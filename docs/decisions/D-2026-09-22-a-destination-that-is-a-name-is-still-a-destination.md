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

- **`--push --all`, and each word of that was a defect.** Plain `git remote get-url` returns the
  *fetch* URL, and `git push` uses `remote.<name>.pushurl` when it is set. The first version was
  therefore wrong in both directions at once whenever they differ: the host that would actually be
  dialled was **missing** from the allowlist — the deployment's own guard refusing its own push,
  which is the outage this change exists to remove — and a host nothing dials was **added** to it.
  Driven against real `git config`: a `pushurl`, a `pushInsteadOf` rewrite and two push URLs all
  resolved to the fetch host before, and to the push hosts now. `insteadOf` was always correct,
  because `get-url` expands it.

- **Only when the checkout is not this process's own**, and asked through a predicate both sides
  share. At `note_repo_dir="."` the writer refuses the write before it can push —
  `git_writer._require_dedicated_checkout` will not commit into the running application's own tree
  and push to the source repository — so there is no destination to allow, and deriving one would
  put the *source* repository's host on every dev checkout's allowlist. **The first spelling was a
  bare `repo_dir == "."` and it did not hold**: driven, `./`, `././`, `src/..`, `$PWD`, an absolute
  path and a symlink all derived this repository's own remote while the writer went on refusing
  them — the exact widening the comment claimed to prevent, on spellings a compose file or a Helm
  values file writes without thinking. The predicate moves to `core/checkout.py`, which
  `git_writer` now calls too, so the two cannot disagree again;
  `test_the_derivation_and_the_writers_refusal_ask_the_same_question` holds them together. `core/`
  rather than beside the writer because `core/` may not import `kg/`, which
  `tests/test_layering.py::test_the_kernel_imports_no_sibling` enforces.

- **A derived host may not carry the compiled layer's separator.**
  `core/netguard_preload.c::parse_allowlist` splits its environment variable on commas while
  `_check` here compares whole strings, so one derived entry containing a comma is *two* allowed
  hosts on the compiled layer and one that matches nothing on the Python layer — a host permitted
  by one layer and refused by the other, which is the divergence the first bullet places this
  function in `derive_allowed` to prevent, reintroduced through the data. Driven: a remote URL of
  `https://harmless,target.example.com/n.git` put `harmless` and `target.example.com` on the
  compiled allowlist. A derived host must now match `[A-Za-z0-9._:-]+`, which also discards the
  junk a multi-line or backtick-bearing URL produces.

- **Absent beats wrong, and nothing here may raise.** No git, no checkout, no such remote, a
  local-path remote, a wedged filesystem past the five-second bound: each contributes nothing. An
  absent entry refuses a push a deployment can still permit through `egress_allow`; a wrong entry
  opens a host nobody declared. `derive_allowed` runs at `chemclaw.core.config` import in every
  process, so an exception is an import-time crashloop — **and the first version could produce
  one**: `_host_from_url` sat outside the `try`, and `urlsplit` raises `ValueError` on an
  unbalanced `[` that `git remote add` accepts (`https://[oops/path`). Every component would have
  failed to start on a `.git/config` a deployment could write by accident.

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
path have no host at all. Driven over eleven spellings including the scp-like `git@host:path`,
which is the form that surprises — it has no scheme, so anything reading it as a URL sees no host
unless asked the right way — an IPv6 literal, which both layers unbracket alike, and a URL carrying
credentials, from which only the host is taken.

**The cache is keyed on the resolved directory, not on the string.** A relative `note_repo_dir`
names different directories under different working directories; measured before the fix, two
clones both reached as `notes` returned the first one's host for the second.

**One case is named and not handled, deliberately.** An SSH host *alias* — `git@notes-alias:o/n.git`
with `Host notes-alias` / `HostName real-git.internal.example` in `~/.ssh/config` — derives
`notes-alias`, while ssh dials `real-git.internal.example`, which is not on the allowlist. A first
draft of this ADR listed that under `Revisit when:` as hypothetical; it is reachable today with one
`git remote add` plus an ssh config, so a refusal that has already expired is not a trigger. It is
not handled because resolving it means a *second* mechanism — `ssh -G <alias>`, a second subprocess,
against a config file this tree does not otherwise read — for a configuration nothing in this
repository ships or tests. A deployment that uses one names the real host in `egress_allow`, the
same way it did for the git remote before this change, and the symptom is the same refusal it had
then. `docs/planning/BACKLOG.md` carries the row.

**Revisit when:** a second destination arrives that is a *name* rather than an address — a registry
alias, a service name resolved from a file, the ssh alias above. At two the pattern is worth
extracting into something a derived guard can see; at one it is a special case with its own tests.
The file that would show it is
`tests/test_netguard.py::test_every_destination_shaped_setting_is_on_the_allowlist_it_derives`,
which by construction cannot find such a destination — it is the guard that could not find this
one either — so what would show it is that test's exemption list growing a second entry.
