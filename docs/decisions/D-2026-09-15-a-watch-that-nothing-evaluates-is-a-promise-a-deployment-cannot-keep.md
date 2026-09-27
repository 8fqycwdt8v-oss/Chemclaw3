# D-2026-09-15-a-watch-that-nothing-evaluates-is-a-promise-a-deployment-cannot-keep — the proactive path shipped off, and nothing said so

**Status:** accepted · **Date:** 2026-09-15 · Builds on
`D-2026-08-27-a-digest-nobody-can-read-is-not-delivered`, whose precondition is now satisfied.

## Context

"Make scientists aware of things, relationships, patterns" is one of this system's stated jobs, and
there is exactly one mechanism for it: `watch_for` saves a standing query, `DigestWorkflow` sweeps
the corpus daily, and `GET /digests` plus the UI's `/review` card deliver what it found.

Three settings decide whether that happens, and `digest_enabled` is read in exactly **one** place —
`durable/schedules.py`, which creates the `digest` Schedule only when it is true. It defaulted
`False`. Nothing else in the tree consults it.

So on every shipped deployment:

1. A chemist asks the agent to watch for something.
2. `watch_for` writes the `subscriptions` row and answers, in the first person:
   *"Watching for 'X'; you'll be told when something new matches."*
3. No Schedule exists. Nothing ever evaluates the row.
4. `list_watches` confirms the watch is there.

The tool reported success, the row was real, and the promise could not be kept. That is worse than
the capability being absent: an absent capability is discovered in one turn, and this one is
discovered by waiting.

## Why it was off, and why neither reason survives

The config comment gave two:

- **"it needs the `subscriptions` table (migration 017)"** — migration 017 ships in `infra/sql/`
  and runs with every other. Any migrated deployment has the table. Stale.
- **"a deployment nobody has subscribed on would just run an empty sweep"** — answered in code
  rather than in config. `digest._match_corpus` returns before `load_notes` when there are no
  subscriptions, and that early return's *own* comment says it exists "because a deployment with no
  subscriptions was paying for it in full". What is left is one daily workflow that does a single
  indexed read and stops.

The condition that genuinely had to hold first is `D-2026-08-27`'s: turning this on while nothing
could *read* a digest lost matches rather than merely failing to deliver them, because the
acknowledgement advanced a watermark `_is_new` can never re-qualify. That reader exists now —
`api/routes/streams.read_digests` and `Chemclaw3_ui`'s `Digests()` card.

## Decision

**`digest_enabled` defaults `True`, and `watch_for` tells the truth when a deployment has turned it
off.**

Both halves, because either alone leaves a defect. Defaulting on without the warning leaves the
silent lie for every deployment that opts out. Warning without defaulting on leaves the capability
off everywhere and merely announces it. The watch is still *saved* when digests are off — an
operator turning them on must have something to deliver against, and `list_watches` must still show
it — but the sentence a chemist reads names the setting instead of promising.

## Consequences

- One Temporal Schedule appears on deployments that had none, doing a single indexed read per day
  where nobody has subscribed. That is the whole cost.
- `.env.example` moves with it, as `tests/test_config.py::test_env_example_ships_the_code_defaults`
  requires — and that guard is what caught this commit's declaration lagging its code.
- A deployment that deliberately wants no digests sets `CHEMCLAW_DIGEST_ENABLED=false` and gets an
  honest tool rather than a broken one.

## What keeps it true

- `tests/test_digest.py::test_a_watch_says_so_when_nothing_will_evaluate_it`
- `tests/test_config.py::test_env_example_ships_the_code_defaults`
