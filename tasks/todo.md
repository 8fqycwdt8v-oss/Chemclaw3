# Multi-wave re-review — waves 4-8 (building on 1-3)

Waves 1-3 (merged: #331, #332, #333) read the tree: per-package, then cross-cutting,
then the regressions the first two introduced. 107 defects fixed. Waves 4-8 escalate
from *reading* the tree to *running* it, attacking it, and measuring it.

Rules carried forward from waves 1-3 (they were earned):
- No fix agent may run `git checkout --`, `git stash`, or `git reset`. A/B by hand-edit.
- Fix agents get disjoint file sets. Cross-scope residuals come back to the orchestrator.
- Every fix must reproduce the defect first, check `docs/decisions/` for an ADR making the
  behaviour intentional, and prove the fix with a test watched failing against unfixed source.
- `.mypy_cache` corrupts under concurrent `mypy` in one directory. `rm -rf .mypy_cache`
  before the gate whenever agents ran in parallel.
- Prose is evidence about what its author believed, never about what the code does.

## Wave 4 — Execution truth: run it, do not read it (MERGED, #334)
- [x] W4.1 Postgres-backed suite driven against the live DB (the ~200 skips), every gate reached
- [x] W4.2 Temporal worker + durable jobs end to end against the running broker
- [x] W4.3 Front door: OIDC-off and OIDC-on, SSE stream, session push-back, budget metering
- [x] W4.4 The live lane (`make live-infra`/`live-up`/`live-probes`) against `chemclaw.cli.mock_llm`
- [x] W4.5 Every CLI entry point and every `make` target actually invoked
- [x] W4.6 Chart render + kubeconform + promtool on shipped defaults and on the refusal paths
- [x] W4 fix stage, gate, PR, merge on green

## Wave 5 — Adversarial input and hostile state (MERGED, #335)
- [x] W5.1 Untrusted text: envelope framing/defanging, nonce, Cf category, helper reports
- [x] W5.2 Authorization chain: every middleware order, refusal paths, subagent attenuation
- [x] W5.3 Manifest/declaration fuzz: connector.yaml, datasource.yaml, sink, SKILL.md, templates
- [x] W5.4 Egress guard + secrets: what escapes, what is logged, what reaches a metric label
- [x] W5.5 Malformed persisted state: half-written rows, unknown shapes, migration gaps
- [x] W5 fix stage, gate, PR, merge on green

### Wave 5 — deliberately left open, with the reason

- **The compaction placeholder stays unmarked.** `SYSTEM_SPEECH_MARK` makes a refusal
  unforgeable; the placeholder does not carry it, so a hostile connector can still write that
  string. Rather than leave an unkept promise, the system prompt now *withdraws* the claim —
  it tells the model the placeholder "carries no mark, so read it as a hint and not as proof" —
  under an absence test. Closing it costs ~26 characters per cleared result, tens of times, in
  exactly the situation where the budget is already spent. A coherent position, not a silent gap.
- **The mark is plaintext in every refusal**, so a model that pastes one into connector arguments
  leaks it, and `framing._defang` has no matching pass. Recorded in the constant's docstring; the
  fix belongs beside `_FORGERY`.
- **The ambient-proxy hole stays open on the repository's defaults** (closed under the shipped
  chart, `entra_required` deciding). The full close was already measured and rejected by
  `D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address` — it refuses a stock checkout
  behind a corporate proxy. Both backlog rows now state the real scope instead of overclaiming.
- **The egress guard cannot see gRPC or Temporal.** grpc's C-core and the Rust sdk-core bypass
  `socket.socket`; no code fix exists here. Both docstrings now say the two settings are allowlist
  entries rather than enforcement, and a backlog row exists where there was none.
- **A wrong value under a right cache key** is not validated: that needs the calculator's schema,
  which lives in `Chemclaw3-mcp`.
- **A bundle's *total* prose is not bounded**, only each field: a 100-job manifest is 100 bounded
  tools. Said plainly in the ADR rather than overclaimed.

## Wave 6 — Concurrency, durability, failure injection (MERGED, #336)
- [x] W6.1 Crash/retry/idempotency across Temporal activities and the connector-job wrapper
- [x] W6.2 Checkpointer + session store under concurrent turns on one thread
- [x] W6.3 Pool exhaustion, cancellation, timeouts; the parent-ceiling invariant under load
- [x] W6.4 Retention/pruning races against live writers
- [x] W6.5 Sinks and publication: at-least-once, duplicate suppression, partial failure
- [x] W6 fix stage, gate, PR, merge on green

## Wave 7 — Numbers: budgets, accounting, cost (MERGED, #337)
- [x] W7.1 Context prefix/floor/compaction arithmetic measured end to end, connectors bound
- [x] W7.2 Token/spend accounting: every counter proved to move, and to move by the right amount
- [x] W7.3 Every number in prose (CLAUDE.md, ARCHITECTURE.md, ADRs, docstrings) re-measured
- [x] W7.4 Hot paths profiled; quadratic growth hunted (checkpoints, ELN re-read, KG scans)
- [x] W7.5 Memory: resident growth, cache bounds, the one eviction policy
- [x] W7 fix stage, gate, PR, merge on green

## Wave 8 — Doctrine, dead weight, and the tests themselves (fixed; gate running)
- [x] W8.1 Every merged ADR's claim checked against the code: does the control exist, is it called
- [x] W8.2 Dead code, settings with no reader, metrics with no producer, one-caller abstractions
- [x] W8.3 Mutation sweep over everything waves 4-7 touched
- [x] W8.4 Repo map / ARCHITECTURE / layering / validator conformance
- [x] W8.5 The four items waves 1-3 deliberately left open, re-decided with measurement
- [x] W8 fix stage, gate, PR, merge on green

## Wave 9 — the register, worked down

Waves 4-8 found and measured the defects. What is left is a register, and a row
that reads as pending forever is the thing `DEFERRED.md`'s own rules forbid. So
this wave closes what is actionable and *decides* what is not — an ADR taking a
posture deliberately is a close; a row nobody ever revisits is not.

Sorted by which they are, because the two need different work:

**Actionable — close them**
- [ ] W9.1 The retention sweep's missing resume watermark (steady-state pass walks the whole table)
- [ ] W9.2 An erasure that races a live turn cannot be completed by re-running it
- [ ] W9.3 `_assemble_graph` rebuilds every node and edge on any corpus change (~1,450 ms at 20k)
- [ ] W9.4 The outbox double-claim, which needs the lease column a comment already describes
- [ ] W9.5 A bulk ELN backfill re-qualifies every ingested file per chunk, writing false rejections
- [ ] W9.6 The nine reference stores no configuration can select
- [ ] W9.7 The system-speech mark is plaintext in every refusal and `_defang` has no pass for it

**Decisions, not patches — take them or record why not**
- [ ] W9.8 `CREATE ON SCHEMA public`: choose the narrower posture or write the ADR keeping this one
- [ ] W9.9 The checkpointer's quadratic WAL — only destructive trimming reaches it, and that
      contradicts a merged decision. Decide it or state the trade in an ADR.
- [ ] W9.10 The sweep/turn write race the read guard only detects — a lock on the turn-serving
      write path is the cost; decide whether it is worth paying.

- [ ] W9 gate, PR, merge on green

Same rules as waves 4-8: disjoint file sets, reproduce before fixing, every fix
ships with a test watched failing, never edit a merged ADR, and no agent runs
`git checkout --`/`stash`/`reset`.

## Review

Five waves (4-8) on top of the first three. Each was reviewed by a fan-out of
agents, fixed by a second fan-out on disjoint file sets, gated on
`make lint type test`, PR'd and merged on green CI.

**The escalation was the point.** Waves 1-3 *read* the tree. Wave 4 **ran** it,
wave 5 **attacked** it, wave 6 **interrupted** it, wave 7 **measured** it, wave 8
audited **the doctrine itself**. Almost nothing found from wave 4 onwards was
reachable by reading — the defects were in what the code did when executed,
under attack, mid-crash, at scale, or in what it claimed about itself.

### The pattern that ran through all five

*A gate that has never been watched refusing is a claim that a gate exists.* It
appeared in every wave and in nearly every form:

- `kg-validate` answering `OK: /etc is a valid knowledge graph` (wave 4)
- `sink-validate` and `channel-validate` green at zero manifests (wave 4)
- `live-probes` exiting 0 in two distinct nothing-happened states (wave 4)
- `restart-postgres` restarting nothing, so a resilience probe reported 24/24
  survivors of a bounce that never happened (wave 4)
- the DDL posture asserted by regex against a file's text, never the ACL (wave 4)
- `_is_auth_failure` reached only from the fetch path while every wording it
  matches is push-side (wave 6)
- the repo-map guard reading only direct children while the document called the
  rule enforced (wave 8)
- and finally the guards **this review itself added**, found by mutation: a
  ceiling-off translation, a boundary never driven at its limit, a budget
  accumulation with no three-block fixture (wave 8)

The last one matters most. The review reproduced the defect it exists to find,
inside its own repair — twice, counting the orchestrator's own vacuous assertion
that compared a module path instead of the imported symbol and accepted exactly
what it was written to refuse.

### Where measurement overturned a reviewer

Six times a fixer disproved the finding it was given, which is the discipline
working rather than failing:

1. The checkpoint read-guard proposal would have refused **every thread in the
   fleet** — consumed channels bump a version with no blob.
2. The routed ELN fix returns an arbitrary subset: the list sorts by filename
   while the window is in the payload.
3. The two-population estimator ratio is correct and was still declined —
   `turn_usage` multiplies a whole-request estimate by that same ratio.
4. A per-term retrieval scan: 92x faster on a non-matching query, 1,718 → 3,075
   ms on a matching one.
5. The namespace caveat came out **opposite** to its prediction — a leak that
   grows with helper use, not an over-prune.
6. The effect ledger's fork was decided as neither offered arm.

### The six-wave impasse, closed

Quadratic checkpoint growth was left open by every previous wave because
`retention.py` stated in two places that in-thread pruning was impossible.
Measured false. A 40-turn thread went from 520 rows and 10.3 MB to 15 rows and
757 kB, resuming byte-identical. It was blocked by a sentence, not a constraint.

### Deliberately open, with the reason recorded

Each has a register row or an ADR paragraph, and each says what would change the
answer: the ambient-proxy hole on repo defaults (a merged ADR already measured
and rejected the full close); gRPC and Temporal being invisible to the egress
guard (no code fix exists — grpc's C-core bypasses `socket.socket`); the
checkpointer's still-quadratic **WAL** (only destructive trimming reaches it, and
that contradicts the compaction decision); `_assemble_graph`'s full rebuild; the
sweep/turn race the read guard only *detects*; an erasure that cannot be
completed by re-running; the narrower DDL posture; the nine `InMemory*` stores;
and the compaction placeholder's unmarked status, where the system prompt now
*withdraws* the claim rather than leaving an unkept promise.

### What held

Worth recording, because a review that only reports defects misleads about the
system. The authorization chain survived a driven compiled graph with **no hole**
— 15 load-bearing claims verified sound. Across 519 ADRs, 430 settings, 367 SQL
columns and 183 metric series there was **no fifth `reject_widening`**: every
governance function an ADR names has a real caller. Zero settings with no reader,
zero metrics with no producer, zero unread corpora. The untested layering rule —
capability code lives in a bundle or in `science/` — holds by hand-audit. And
CLAUDE.md's subagent arithmetic, the paragraph most likely to have rotted,
measured correct on every figure.
