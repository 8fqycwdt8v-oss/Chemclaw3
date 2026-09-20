# Hypothesis tournament — parallel generation, evidence-seeded Elo, discriminating experiments

**Status:** implemented; under review. `make lint type` green.
**Scope chosen:** full feature + ADR. Elo seeded by evidence checks.

The previous occupant of this file was the ten-wave review's four follow-ups, all four closed
and merged (#420, #421, #422). It is moved to `docs/archive/plans/ten-wave-followups.md` rather
than discarded, because its review section is the record of that work — the same reason the
peer-handoff plan was archived before it.

## The ask

Generate hypotheses with many parallel agents; critique them so only valuable ones survive; rank
them by Elo; return each with a discriminating experiment — executed automatically where the
system's own tools can settle it, proposed where a human must run it.

## What already exists (do not rebuild)

| Stage | Reuse |
|---|---|
| Parallel fan-out | `task` fans out today (`agent/subagents.py:37`); deterministic `Send` precedent in `retrieval/fanout.py:282-290` |
| Evidence | `gather_evidence` (`agent/research_tools.py:330`) over `retrieval.fanout.sweep_sources` |
| Judging | `agent/verifier.py:409` `judge_once` — json_schema structured output, injection-hardened prompt (`:209`), median-of-rerolls (`:472`) |
| Deterministic critique | `protocols/checks.py` — 18 computed verdicts, incl. `no_documented_failure` (`:907`), `precedent_consulted` (`:956`) |
| Contradiction | `kg/conflicts.py::find_conflicts` (`:352`) — declared + suspected |
| Gap seeding | `kg/analytics.py::analyze` → `GraphGaps` — its own docstring says it should seed proposals, and nothing calls it from that path |
| Experiment output | `experiment-proposal` note type (`kg/note.py:447`), written via `kg/record.py` |
| Numeric ranking | `suggest_next_experiment` + Pareto `front` (`connectors/bo/server/tools.py:120`) |
| Durable pattern | `durable/` workflows + `agent/durable_tools.py` job-handle shape |

## What is missing

1. No `Hypothesis` type — nothing represents a hypothesis as a comparable entity with a
   refutation condition.
2. No multi-candidate generation. Every path caps at one by design.
3. No critic agent (deleted 2026-08-15).
4. No pairwise comparison anywhere. `evals/ab.py` is paired-*absolute*, not pairwise-preference.
5. No rating store.
6. No discriminating-experiment computation — the idea exists only as prose in
   `skills/experiment-progression/SKILL.md` §5.

## Constraints that shape the design

- **C1 — loop cap.** `harness_max_loop_iterations = 25`, shared turn-wide across every fan-out
  branch (`agent/loop_cap.py:210-236`). 10 hypotheses = 45 round-robin comparisons. A turn cannot
  hold this. **The tournament is a Temporal workflow.**
- **C2 — helpers cannot act.** A helper's surface is `caller - side_effecting_tools()`
  (`agent/subagents.py:495`), and `compute_xtb_energy` is in that set. Helpers cannot calculate and
  cannot write notes. In Temporal this constraint disappears: activities call the calc server
  directly.
- **C3 — prefix ceiling.** `task` is at 897/900 tokens (`core/config/agent.py:812-818`). Prefer a
  single new tool that *starts a job* over new rostered helper profiles.
- **C4 — concurrency 8.** `agent_max_parallel_tool_calls = 8`; N>8 runs in waves.
- **C5 — the deleted panel.** `D-2026-08-15` deleted 1,442 lines of exactly this, and names the
  terms for return: "isolation and parallelism do [justify a spawn]". This qualifies, but it is a
  new decision.
- **C6 — the measured null result.** `D-2026-08-16`: 39 flagged answers revised, 10 cleared, null
  control cleared 2.0 per roll — benefit over doing nothing **zero**, and 8 of 10 clears were
  deletions. "Any loop scored on flag clearance learns exactly that move."
- **C7 — the discarded score.** `science/bo/engine.py:323` refuses to surface the acquisition score
  because it would be misread as a confidence.

## Design

### Layer 2 owns it: `durable/hypothesis_tournament.py`

A workflow on `background-jobs`. Layer 1 gets **one** tool,
`run_hypothesis_tournament(question, ...)`, returning a job handle in the existing
`agent/durable_tools.py` shape. Rationale: C1 and C3.

### Stage 1 — generation (parallel activities)

N activities, each `build_chat_model` on a distinct **angle** brief, angles generated for the
question rather than declared — this is `D-2026-08-13`'s own finding, which the deletion ADR did not
overturn: a fixed persona list "is wrong in both directions".

Seed the angles from `kg/analytics.analyze` (`GraphGaps`) so generation starts from what the record
does *not* know, closing missing-piece #7.

Output: `Hypothesis` — statement, mechanism, `refuted_if` (required, non-empty), cited note ids.

### Stage 2 — filter, on mechanical grounds only

**Not a model scored on rejection** (C6). The gate rejects only what a rule can see:
- `refuted_if` empty or not falsifiable by any stated observation → reject;
- near-duplicate of a surviving hypothesis → merge (never prose-similarity *mint*, per `D-162`'s
  rule that a pattern-matched motive is indistinguishable from testimony — dedup is allowed,
  invention is not);
- contradicted by a recorded `failure-mode` note → demote and annotate, **not** reject
  (`protocols/checks.py:907`'s rule: "A recorded failure is evidence and not a verdict").

A model critic runs *after* this, and its output is an attached objection with a rationale, which
becomes evidence in the tournament — it never silently removes a candidate. Corroboration without a
stated rationale does not count, per `runner_answer`'s existing rule.

### Stage 3 — evidence-seeded Elo

- Swiss pairing, not round robin: ~N·log2(N) comparisons instead of N(N-1)/2.
- Each comparison activity: `gather_evidence` on both hypotheses, plus any cheap computed value
  already cached (D-011 means a repeat is free), handed to the judge inside
  `framing.ENVELOPE_TAG` with ids through `safe_id` and both statements `defang`ed — the exact
  hardening `verifier._verifier_prompt:209` already uses.
- **Order randomized per comparison**, both orders judged where budget allows, to measure position
  bias rather than assume it away.
- K-factor fixed; ratings stored with **comparison count and standard error**. Report is
  `rating ± se (n comparisons)`, never a bare number — this is the answer to C7.

### Stage 4 — settle what can be settled

For each surviving hypothesis, derive the discriminating check. If it is computable with tools the
system holds (xTB energy, pKa, solubility, site reactivity, hazard screen), **the workflow runs it**
and the result lands in the record — which is what "it should simply happen" asks for, and is safe
here because a Temporal activity is not a helper (C2). If it requires the lab, write an
`experiment-proposal` note through `kg/record.py` in the §5 shape already in use: conditions,
rationale, falsifiable expectation, fallback, stated confound.

### Output to the chemist

Ranked table — hypothesis, Elo ± se, n comparisons, the objection that survived, the discriminating
check and whether it was run or proposed. Plus **the one to do next**, named. That is how this
reconciles with `skills/experiment-progression` §5's "a list of five is a way of avoiding the
question": the list is the reasoning made auditable, the top-1 is still the recommendation.

## Measurement (this is what killed the last two attempts — it ships with the feature)

An ADR here must carry a measurement, not an argument. Backtest against the ELN corpus, where
outcomes are already recorded:

1. Take `optimization-campaign` notes with a known outcome. Truncate the series before the decisive
   run. Generate hypotheses. Ask whether the Elo-top-1 matches what actually worked.
2. **Null control, from `D-2026-08-16`'s lesson:** compare Elo ordering against (a) random ordering
   and (b) generation order. If Elo does not beat both, the tournament is decoration and the ADR
   says so and the feature does not ship on.
3. Report position-bias rate from the both-orders comparisons.

## Steps

- [x] 1. ADR `D-2026-09-20-<slug>.md` + ledger row. Must answer: C5 (re-opening the panel), C6 (why
      selection differs from prose-revision), C7 (what an Elo means and does not), and the tension
      with `experiment-progression` §5. Carries `Revisit when:` for anything declined.
- [x] 2. `Hypothesis` model + `hypothesis` note type in `kg/note.py`; rating store.
- [x] 3. `durable/hypothesis_tournament.py` — workflow + activities. Register in `durable/registry.py`.
- [x] 4. Elo: pairing, update, standard error. Pure function, unit-tested with known sequences.
- [x] 5. Evidence-seeded comparison activity, reusing `verifier.py`'s hardened prompt shape.
- [x] 6. Stage-4 settle: computable → run; physical → `experiment-proposal` note.
- [x] 7. `run_hypothesis_tournament` tool + authz classification (state-changing: it writes notes).
- [x] 8. `ARCHITECTURE.md` row + `README.md` for any new directory (`tests/test_repo_map.py`, D-156).
- [x] 9. Backtest eval + null control under `evals/`.
- [x] 10. `make lint type test` green, with the Postgres daemon started so the durable tests
      actually run (`sudo -n dockerd &`, `make up`, `make db-migrate`).

## Review

(to fill in on completion)
