# D-2026-09-26-a-checkpoint-refuses-only-what-a-node-would-index — which absent channels refuse a resume

**Status:** accepted · **Date:** 2026-09-26 · **Supersedes**
`D-2026-08-13-a-checkpoint-says-which-schema-wrote-it` §3's name comparison (the stamp, the
refusal's type and message, and its §4-§5 are unchanged). Closes the `BACKLOG.md` row *"A peer that
is genuinely restorable still drains every live session on the deploy that adds it"* (issue #466).

## Context

`SchemaStampedSaver` stamps each checkpoint with the restorable first-party channels its build
declared, and on resume refused any stamp missing a channel the current build declares. The failure
that refusal pre-empts is measured and still reproduces
(`tests/test_checkpointer_schema.py::test_a_moved_channel_strands_a_turn_resumed_inside_the_graph`):
a node that *indexes* a channel an older checkpoint never held raises a bare `KeyError` mid-turn.
The same file measures the other half
(`test_notrequired_does_not_make_an_added_channel_safe`): the same absent channel read with
`.get()` resumes perfectly. So the thing that decides the damage is how a channel is read, and the
comparison was over names.

The cost fell on this repository's own deploys: adding `active_agent` refused the next ordinary turn
of every live session, peer mesh on or off, although its one reader takes it with `.get()`. That was
patched with `resumes_when_absent = True` on `state.LastPeer` — a declaration, on the channel's
type, of a fact about *other* modules' code, which stays true only until someone adds an indexing
reader three files away.

## Decision

**1. Stamp as before; refuse only an absent channel something indexes.** The stamp still records
every restorable channel the writing build declared (a later build may start indexing one, and it
must be able to tell the checkpoint held it). What changed is the refusal set: of the channels the
stamp lacks, only those `checkpointer.channels_read_without_default` returns are refused.

**2. Derived from the source, not declared.** Every string constant equal to an absent channel's
name in this package's own modules (`SOURCE_ROOT`) is classified by the node it sits in. A closed
list is safe: `.get(name…)`, `.setdefault(name…)`, a subscript *store* or *delete*, a dict-literal
key (an update), an `in`/`not in` test, a bare expression. Everything else is an index.
`resumes_when_absent` is deleted.

**3. Fail closed, because the two errors are not symmetric.** Calling a channel indexed when it is
not costs the named, actionable refusal this guard always gave. Calling it safe when it is not is
the bare `KeyError` the guard exists to pre-empt. So an unrecognised occurrence (a name in a tuple,
passed to `itemgetter`, bound to a variable), an unreadable or unparseable module, and a tree with
no source at all each count as an index. Driven end to end on Postgres: a resumed node reading the
new channel through `itemgetter` is refused by name at the load, and the same resume with the
derivation pointed at a defaulted reader is the bare `KeyError` — the control that proves the arm
reaches the failure.

**4. Only on a deploy transition.** The scan runs when a stored stamp lacks a declared channel, and
is cached per (names, tree), so an ordinary turn never reaches it. Measured on this package, the
substring pre-filter leaves six modules to parse of 523.

## What was rejected

- **Keep the hand declaration (`resumes_when_absent`).** It is the row's own complaint: a claim
  about readers written on the channel, which nothing re-checks when a reader changes.
- **A test-time derivation that checks the declaration.** Still a hand list with a guard, and the
  guard would red on a legitimate new indexing reader rather than simply refusing, which is the
  correct runtime behaviour for that build.
- **Seeding a default into an absent channel on restore.** A `LastValue` has no meaningful default
  to seed, and a silent default is the confidently-wrong resume `D-2026-08-13` §4 refuses.
- **Stamping only indexed channels.** It makes a build that starts indexing a channel refuse
  checkpoints that *held* it, because the older stamp never recorded the name.

## What it does not catch

A channel name spelled at run time (`"active" + "_agent"`), which no reader here does; a reader
outside this package (upstream and middleware cannot name a first-party channel); and everything
`D-2026-08-13` §3 already listed (type changes, upstream channels).
