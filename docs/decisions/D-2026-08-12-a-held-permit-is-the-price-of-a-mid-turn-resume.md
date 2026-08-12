# D-2026-08-12-a-held-permit-is-the-price-of-a-mid-turn-resume — Mid-turn resume stays off, because the push-back mailbox already answers its question for free

**Status:** accepted · **Date:** 2026-08-12 · Decides the `_resume` row left open by the LangGraph migration.

## The question that was actually open

`mid_turn_resume_enabled` ships off with the wiring complete (`api/runner.py`, gap AGT-2): when a
turn launches a durable job, wait up to `mid_turn_resume_timeout_seconds` for the result and
continue the *same* turn with it, so "compute this, then reason about the result" is one exchange
rather than two. The migration hand-off recorded the decision as unmade rather than the code as
missing.

It is decidable now, and not by writing anything.

## Two mechanisms, one question

The system already answers "the chemist learns the job finished" twice:

- **Mid-turn resume** holds the turn open and therefore holds an **admission permit** —
  1 of `service_max_concurrent_turns`, default 8, per process — for up to 60 s.
- **The job→session push-back mailbox** (F3) re-wakes the session with the same result and holds
  nothing while the job runs.

The second was built for exactly this and costs no capacity. That is the whole comparison.

## What the permit costs, measured

M12 probe (a), the admission sweep, run 2026-08-12 against Postgres + Temporal + the front door on
`cli/mock_llm`, 24 concurrent offered, 48 turns per step, the door restarted at each cap:

| cap | accepted | shed | p50 s | p95 s | answered/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 10 | 38 | 6.0 | 6.7 | 0.71 |
| 4 | 17 | 31 | 7.0 | 9.1 | 1.02 |
| 8 | 24 | 24 | 8.1 | 11.8 | 1.14 |
| 16 | 37 | 11 | 13.4 | 20.9 | 1.27 |
| 32 | 48 | 0 | 18.7 | 31.9 | 1.30 |

A normal turn on this lane holds its permit for a **p50 of 8.1 s** at the shipped cap of 8. A resume
may hold one for **60 s** — roughly **seven turns' worth of one of eight permits**, spent waiting on
a job whose result the mailbox would have delivered anyway. Under the offered load above, the door
was already shedding half the turns at cap 8; adding a class of turn that occupies a permit seven
times as long is a throughput decision, not a UX one.

## The decision

**`mid_turn_resume_enabled` stays `false`, and the reason is now recorded rather than assumed.** The
capability stays because a deployment with spare admission and a strong preference for single-turn
exchanges is a real case, and the config comment already bounds it below the turn deadline.

What changes is the justification. It used to read "holding a turn open holds an admission permit,
so a deployment opts in deliberately" — true, and silent on the fact that a cheaper mechanism
already exists. A deployment turning this on should know it is choosing the expensive answer to a
question the mailbox answers for nothing.

## What the sweep also settled, and what it did not

**Settled:** the D-123 shape is gone. Its defect was that 8 concurrent turns failed on a shared
client and 0 failed on per-turn ones; the parser that caused it went with the dependency, but
LangGraph's own behaviour at that load was unmeasured. At cap 8 the door accepted 24 turns with
**zero unaccounted for**, and goodput rises monotonically with the cap (0.71 → 1.30 answered/s), so
the per-turn graph build holds up and the cap is load-bearing.

**Not settled:** the knee. Within-cap spread measured **28 %**, above `_MAX_READABLE_NOISE`, so
`_knee` correctly refuses to name one and two of the four checks fail on that basis. Both failures
are about the resolution of the *sweep*, not the behaviour of the system — the harness declining to
read a knee out of its own noise is `D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with`
working. A quieter box, or more samples per cap, is what would resolve it; this one was also
running Postgres, Temporal, four workers and the mock.
