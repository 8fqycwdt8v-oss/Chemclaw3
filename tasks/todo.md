# Backlog implementation pass — 2026-09-04

Working `docs/planning/BACKLOG.md` top-down, implementing the rows that are actually
implementable offline. Every row was re-checked against `HEAD` before being queued
(BACKLOG.md's own rule); two of them turned out to state something false about the tree,
and correcting the row is the deliverable there rather than code.

Owner decisions taken by the user this session are recorded per item.

## Queue

- [x] **A — [H] A helper's scratch file crosses to its caller unframed** (BACKLOG §4)
      Owner decision: **keep the crossing, defang on read.**
      Measured: caller `read_file` returns 10,024 chars carrying a live `</retrieved-note-…>`;
      three further arms found (`grep --output_mode=content`, a crafted *path* echoed by
      `write_file` with no helper at all, and the file surviving into a later turn on the
      same `thread_id`). Third treatment in `frame_connector_results`, keyed on
      `scratchpad_tools()` so an upstream-added verb is covered. Needs an ADR.

- [x] **B — A schedule whose every run is killed reads as healthy** (BACKLOG §3)
      Row's "needs a live broker" premise is **false**: driven end to end against
      `WorkflowEnvironment.start_local()` in this sandbox, two runs killed by their own
      execution timeout report byte-identically to healthy, and one extra `describe` per
      recent action recovers `status=TIMED_OUT`.

- [x] **C — Ten `KNOWN_OVERSIZED` tools are one defect wearing ten names** (BACKLOG §5)
      Row's premise is **false, measured**: installed `langchain_core` 1.6.0 has no
      `$defs` switch (`_convert_pydantic_to_openai_function` dereferences and pops
      unconditionally), and a hand-built `$defs` schema costs **+31 tokens net** across the
      ten. Deliverable is the corrected row plus asserting the drifted per-tool numbers.

- [x] **D — The module a chemist reads has no test file** (BACKLOG §5)
      Row's own closing condition is met: `render.py` coverage **76% → 94%**. Delete it.

- [x] **E — The corpus drain is the one ingest pass with no metric** (BACKLOG §4)
      Also closes the non-gated half of the stalled-feed row.

- [x] **F — Two readers bypass `external_record_id`** (prerequisite of BACKLOG §2's
      fingerprint-citation row)

- [x] **G — A truthful `stated` quote from an earlier turn cannot be represented** (BACKLOG §5)
      Must land *with* a bound on the history read, or it trades a correctness bug for an
      unbounded per-turn scan the store's own docstring forbids.

- [x] **I — No deployment declares a context window** (BACKLOG §4)
      Owner decision: **window-aware arm on the indicator only**; leave what
      `agent_context_token_budget` means alone.

- [x] **H — A second sign-off at the same revision overwrites the first** (BACKLOG §5)
      Needs the `Chemclaw3_ui` PR in the same change or it is a docstring-only control.

- [x] **J — `predict_reaction_conditions` is unreachable from any deployment** (BACKLOG §5)
      Owner decision: **wire it, on by default.** Needs an ADR for the enablement default.

## Not taken, and why

Deployment-blocked (no cluster / no results-store target / no warehouse): the chart-per-repo
row, Postgres+Temporal ownership, the live results sink, corpus volume, the published-calc
cross-reference, `read_corpus`'s full scan, the append-only staleness gauge (no shipped
binding sets `append_only`).

Budget-blocked: ChemRAG (1,932 pairs), the 288-probe A/B, the delegation corpus, the profile
allow-list re-measure. The credential works this session (probed: 8 in / 1 out on haiku), but
each of these is hundreds to thousands of calls.

Closed by decision already in the row: the two-row delete ordering ("keep both orders"), the
second roster name ("leave it closed").


## Review — batch 1 (merged as #304)

`make lint type test` green at 6397 passed / 0 failed; CI green on all four checks.

**What the reviews were worth.** The fresh-context review of A found six things, two of
which no amount of care by the author would have caught: the ADR asserted two `BACKLOG.md`
rows that did not exist (the exact silent omission its own sentences denied), and arm 1's
headline measurement came from a fixture nothing drives. Both are the failure mode
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` exists to stop, reintroduced by
the change that extends it.

**Two rows were false about the tree**, which is why re-checking a row against `HEAD` before
working it is the rule rather than a courtesy:
- the schedule row's "recovering the status costs one describe … which is why it was not
  taken here" reads as blocked on a live broker; `start_local()` runs offline here.
- the `$defs` row's premise (measured separately: no switch, and `$defs` costs tokens rather
  than saving them).

**Twice the obvious implementation was wrong in the direction that hides the defect** —
`first_execution_run_id` reports a killed `continue_as_new` drain as normal, and keying the
defang on `read_file` closes one channel of five. Both were caught by driving the real shape
(a three-hop chain; all six verbs) rather than the minimal one.


## Review — batches 2 and 3 (#305, #306, Chemclaw3_ui#62)

All ten queued rows done. Backlog 44 -> 38 open rows: eight closed, two opened
where an ADR had claimed rows that did not exist.

**Six of the ten rows asserted something false about the tree**, which turned the
preamble's instruction ("if it is wrong, the fix is to correct or delete the row, and
that is as much a contribution as the code would have been") from an edge case into the
majority case:

- the schedule row said it needed a live broker; `start_local()` runs offline here
- the corpus row asked for three outcomes over two populations
- the `$defs` row's premise *and* its headline example were both false
- the context-window row's third fix cannot fire, and my own briefing repeated it
- the `rxnpredict` row said it raises the context floor; the delta is exactly zero
- the sign-off row's anchor named the wrong file

**Twice the obvious implementation was wrong in the direction that hides the defect.**
`first_execution_run_id` reports a killed `continue_as_new` drain as normal; keying the
defang on `read_file` closes one channel of five. Both were caught by driving the real
shape — a three-hop chain, all six verbs — rather than the minimal one.

**Three tests were prose rather than guards, and one went vacuous rather than red.**
The corpus partition test argued in its docstring that a third outcome would break the
partition, and that mutation passed 18 tests. Two guards enumerated a set the tree owns
(`{chem, safety}`; the seven chart bundles) and reported a broken guard when an eighth
arrived. `test_a_fan_out_never_loses_its_own_results` read its trigger off a shipped
setting and stopped exercising a clearing when that setting rose — caught only by its
own "this test proves nothing" guard.

**What the reviews were for.** Not one review found broken code; every implementation
passed its own gate. What they found was false sentences and tests that do not guard
what they claim — an ADR citing backlog rows that did not exist, a measurement taken
from a fixture nothing drives, a docstring justification that measures false, an
operations guide naming the wrong table. That is the class nothing goes red for.

## Left open deliberately

`agent_tool_result_clear_trigger` is now 73,500 and asserted to clear the ratchet
ceiling; if a tool surface is ever allowed past it, that test names the trade rather
than the behaviour changing quietly. `helm` and `kubeconform` both install in this
sandbox, so the 14 chart skips are a default rather than a limit — worth remembering
before reporting a run as green. `infra/live/processes.sh` does not start `rxnpredict`,
so `make live-up` would meet an unreachable connector on probes pr-01..pr-06; starting a
torch-backed predictor there is a decision about checkpoints, not a wiring omission.
