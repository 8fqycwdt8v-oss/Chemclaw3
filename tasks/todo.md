# Backlog implementation pass — 2026-09-04

Working `docs/planning/BACKLOG.md` top-down, implementing the rows that are actually
implementable offline. Every row was re-checked against `HEAD` before being queued
(BACKLOG.md's own rule); two of them turned out to state something false about the tree,
and correcting the row is the deliverable there rather than code.

Owner decisions taken by the user this session are recorded per item.

## Queue

- [ ] **A — [H] A helper's scratch file crosses to its caller unframed** (BACKLOG §4)
      Owner decision: **keep the crossing, defang on read.**
      Measured: caller `read_file` returns 10,024 chars carrying a live `</retrieved-note-…>`;
      three further arms found (`grep --output_mode=content`, a crafted *path* echoed by
      `write_file` with no helper at all, and the file surviving into a later turn on the
      same `thread_id`). Third treatment in `frame_connector_results`, keyed on
      `scratchpad_tools()` so an upstream-added verb is covered. Needs an ADR.

- [ ] **B — A schedule whose every run is killed reads as healthy** (BACKLOG §3)
      Row's "needs a live broker" premise is **false**: driven end to end against
      `WorkflowEnvironment.start_local()` in this sandbox, two runs killed by their own
      execution timeout report byte-identically to healthy, and one extra `describe` per
      recent action recovers `status=TIMED_OUT`.

- [ ] **C — Ten `KNOWN_OVERSIZED` tools are one defect wearing ten names** (BACKLOG §5)
      Row's premise is **false, measured**: installed `langchain_core` 1.6.0 has no
      `$defs` switch (`_convert_pydantic_to_openai_function` dereferences and pops
      unconditionally), and a hand-built `$defs` schema costs **+31 tokens net** across the
      ten. Deliverable is the corrected row plus asserting the drifted per-tool numbers.

- [ ] **D — The module a chemist reads has no test file** (BACKLOG §5)
      Row's own closing condition is met: `render.py` coverage **76% → 94%**. Delete it.

- [ ] **E — The corpus drain is the one ingest pass with no metric** (BACKLOG §4)
      Also closes the non-gated half of the stalled-feed row.

- [ ] **F — Two readers bypass `external_record_id`** (prerequisite of BACKLOG §2's
      fingerprint-citation row)

- [ ] **G — A truthful `stated` quote from an earlier turn cannot be represented** (BACKLOG §5)
      Must land *with* a bound on the history read, or it trades a correctness bug for an
      unbounded per-turn scan the store's own docstring forbids.

- [ ] **I — No deployment declares a context window** (BACKLOG §4)
      Owner decision: **window-aware arm on the indicator only**; leave what
      `agent_context_token_budget` means alone.

- [ ] **H — A second sign-off at the same revision overwrites the first** (BACKLOG §5)
      Needs the `Chemclaw3_ui` PR in the same change or it is a docstring-only control.

- [ ] **J — `predict_reaction_conditions` is unreachable from any deployment** (BACKLOG §5)
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
