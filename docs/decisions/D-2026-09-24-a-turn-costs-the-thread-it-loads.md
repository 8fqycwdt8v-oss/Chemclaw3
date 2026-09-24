# D-2026-09-24-a-turn-costs-the-thread-it-loads — bounding a conversation in the unit that kills the front door

**Status:** accepted · **Date:** 2026-09-24 · Closes the `BACKLOG.md` row *"Nothing bounds what a
turn costs the front door's memory"*, opened by
`D-2026-09-18-a-second-process-in-the-pod-is-memory-the-chart-never-declared`.

## Context

`resources.service` was sized against a front door with **no turn in flight**, and the row asked
for the missing term: drive a turn against the mock LLM and express its peak as MiB per admitted
permit, so `test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares` could take a
third term.

## What was measured

The real front door — `uvicorn chemclaw.api.app:create_app --factory` with the live lane's exact
argv and environment, every connector and worker up — inside its own memory cgroup, every other
lane process outside it. The quantity is the cgroup's **anonymous** charge (`memory.stat
total_rss`, sampled every 10 ms): cgroup v1's `max_usage_in_bytes` folds in page cache, which here
is ~230 MiB of mostly inactive library pages. Turns were driven through `cli/live_storm.storm`.

**Three terms, and the row expected one.**

| Term | Measured | Runs |
| --- | --- | --- |
| Idle, no turn | 314.6 MiB anon | 5, to 0.2 MiB |
| Warm-up, once: the first 50–100 turns at concurrency 1 | +65 to +67 MiB, then flat to 1 MiB over 250 more | 5 |
| Per permit, short thread: concurrency 1 → 4 → 8 → 12 | 5.1–6.1 MiB each, retained as allocator high-water | 3 |
| Offered 16 and 24 | +0 — the excess refused `at_capacity` | 1 |
| Six parallel calls; a forty-call flood | +0 once warm | 1 |

**The one that matters is the fourth.** A turn loads its *whole* checkpointed thread: compaction
trims what is sent to the model and leaves state intact, and `aget_tuple` has no window. So what
an admitted turn costs the pod grows with the conversation it continues:

| Twelve concurrent threads of 95,000-character messages | Pod anon |
| --- | --- |
| warm, before | 437 MiB |
| turn 50 | 854 |
| turn 100 (10.05 MB stored per thread) | **1,268** — the front door alone, no upload anywhere |

Re-driven under a real 1 GiB cgroup limit — the shipped `resources.service.limits.memory` — the
kernel killed the front door at **turn 76** (`oom_kill 1`, `anon-rss:1037284kB`), and all twelve
streams ended mid-body. Nothing refused any of it: the only bound on a thread's length is
`budget_max_turns_per_session`, which is counted **in process** in an LRU, so a restart or a second
replica hands every thread a fresh hundred.

The per-byte coefficient follows character width, for the reason the parse coefficient does
(`D-2026-09-19-a-ceiling-on-the-archive-is-not-a-ceiling-on-the-parse`): CPython holds a `str` at
its widest code point while the stored blob stays UTF-8. Pod bytes per stored byte, per permit:

| Thread | Coefficient |
| --- | --- |
| short tool-calling turns, 84 messages | ~1 |
| one em dash per 95,000-char message | 7.2 |
| one U+1F9EA per 95,000-char message, end of a 30-turn run (2.91 MiB) | 11.7 |
| the same, a second run, rounds 3–21 (0.29–1.95 MiB) | 13.4 → **17.6** |

The two astral runs reached the same 847 MiB a thread apart: allocator high-water moves ~50 MiB
between runs, so the ratio is noisy, and the constant is the largest one measured at or under the
ceiling rather than a fit.

And the existing inequality was already false before any of this: 432 floor + 91 forkserver +
~70 warm-up + ~72 permit high-water + 448 for two parses at 160 MiB is ~1,113 against 1,024, and
the warm front door (665) sat over its 640Mi request from its first busy minute.

## Decision

**1. A conversation is bounded in the unit that kills the pod: `session_max_thread_bytes`.** The
raw size of the `messages` blob the thread's newest root checkpoint points at
(`agent/checkpointer.stored_thread_bytes`), checked after the admission permit beside the session
budget (`api/budget.check_thread_size`). At or over it the turn is refused on the open stream as
`budget_exhausted`, not retryable, telling the chemist to start a new session — the code already
means "this session was refused before the turn started", so no surface needs to learn a new one.

- **Read off the stored thread**, so every replica and every restart sees the same number. That is
  the property the turn caps lack and the reason they cannot stand in for this.
- **The newest copy, not a sum**: `checkpoint_retain_per_thread` keeps superseded copies beside it,
  and no turn loads those.
- **`octet_length` over the `bytea`** reads the TOAST header, so the check is an index probe, not a
  detoast of the thread it is measuring — and it runs on the checkpointer's pool directly, never
  through the saver's process-wide lock.
- **Not behind `budget_enabled`.** It is a memory bound: a deployment that meters no spend still
  runs twelve permits in one container. `0` disables it.
- **A database that cannot answer admits the turn**, counted on
  `chemclaw_degraded_total{subsystem="thread_size"}`; the load that follows reads the same
  database, so refusing would add an outage rather than prevent one.

**2. 1.5 MiB, derived downwards from the pod.** With the coefficient rounded up to 18 and twelve
permits, every MiB of ceiling costs 216 MiB of pod; 2 MiB fails the inequality by 9 MiB, so it is
rounded *down*, the way the parse budget was. At 1.5 MiB a thread holds about three full context
windows (`agent_context_token_budget` is 118,700 tokens) — hundreds of ordinary turns, or fifteen
pastes at `service_max_message_chars`.

**3. `resources.service` goes to 768Mi / 1536Mi.** The request covers the warm front door —
floor, forkserver, warm-up and every permit's high-water, 665 MiB. The limit covers the idle pair,
every permit at the thread ceiling and both parses at their budget: 523 + 466 + 448 = 1,437 of
1,536. The parse budget stays at 160 MiB — the limit moved, not the uploads a chemist can make.

**Driven end to end, not argued.** The same front door on this code, in a cgroup limited to
1,536 MiB, twelve astral threads offered thirty turns each: every thread was admitted through turn
21 and refused from turn 22 on, anon went flat at 847 MiB the moment the refusals began, and the
process was alive at the end with `oom_kill 0` — where the shipped 1Gi limit had killed it at
turn 76. (That run's ceiling was 2 MiB, the value before the second astral run moved the
constant; 1.5 MiB refuses earlier and holds less.)

`tests/test_deploy_chart.py` holds all of it as the inequality it already was, with three measured
constants — `TURN_WARM_MIB`, `TURN_MIB_PER_PERMIT`, `POD_BYTES_PER_THREAD_BYTE` — and the permit
count and thread ceiling read the way a container resolves them. Raising either, or lowering
either declaration, fails there.

## Alternatives

- **Window what a turn loads.** The better end state: a turn would load what compaction would
  send and the per-permit term would be bounded by `agent_context_token_budget` instead of by a
  new cap. Declined for now because state is non-destructive by decision
  (`D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has`) and LangGraph restores a
  checkpoint whole; a partial load is a change to what the checkpointer *is*, and wants its own
  wave. **Revisit when:** a chemist's real thread reaches `session_max_thread_bytes` — visible as
  `chemclaw_turns_refused_budget_total` moving on sessions whose turn count is under
  `budget_max_turns_per_session` — or when LangGraph ships a windowed `aget_tuple`.
- **Resize for short turns only and leave the long thread as a row.** Rejected: the OOM was
  reproduced end to end, under the shipped limit, from twelve legal conversations.
- **Lower the parse budget instead of raising the limit.** Rejected: it would refuse uploads that
  parse today to make room for a term that has its own bound now.
- **Lower `budget_max_turns_per_session`.** Rejected: turns are not the unit — one turn can store
  100,000 characters and another 400 — and the count resets with the process.

## Consequences

- A conversation over 1.5 MiB of stored thread is refused until the chemist starts a new session.
  The transcript stays readable.
- Each front-door replica reserves 128 MiB more memory and may burst to 512 MiB more; at
  `maxReplicas: 6` that is 768 MiB more reserved.
- **Retained high-water sits between request and limit.** A front door that has served long
  threads keeps what they cost, up to ~1 GiB, above its 768Mi request — the normal burstable
  shape, and the one that makes it an early eviction candidate under node pressure.
- Not measured: a background worker taking turns through `template_activities.run_agent_step`,
  which loads threads the same way. Its request and limit are four times the front door's and it
  runs no permits, so it is not the pod this ADR re-derives.

## What keeps it true

- `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`
- `tests/test_thread_size.py::test_the_stored_size_is_the_newest_blob_a_turn_would_load`
- `tests/test_thread_size.py::test_the_front_door_refuses_an_oversize_thread_as_a_spent_session_budget`
- `tests/test_thread_size.py::test_it_binds_with_budgets_off_and_is_disabled_only_by_its_own_zero`
