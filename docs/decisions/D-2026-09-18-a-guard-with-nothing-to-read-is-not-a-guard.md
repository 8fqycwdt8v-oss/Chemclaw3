# D-2026-09-18-a-guard-with-nothing-to-read-is-not-a-guard — the distiller, the profile proposer, and the record the self-confirmation guard needed

**Status:** accepted · **Date:** 2026-09-18

## Context

`D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool` built the queue and gave it one
proposer: the agent, in the turn where the procedure was worked out. This is the other half of that
plan — the proposers that read *history* rather than the turn in hand.

The trajectory miner has been designed since
`D-2026-08-27-count-the-trajectories-before-building-the-distiller`, which set the order deliberately:
measure the corpus before building the miner. `chemclaw/cli/trajectory_census.py` is that
instrument, it already mines recurring tool sequences and recurring failures, and it already
evaluates the stated greenlight. What it never had was a consumer, because until the queue existed
there was nowhere for a proposal to go.

The plan also carried a requirement that had never been built and had, on inspection, nothing to
build against: **nothing distilled may count evidence it itself produced**, with the guard shipping
beside its caller.

## The finding

**The guard had no producer, and the plan did not know it.** A skill, once accepted, is injected
into the prompt and *shapes the trajectories that follow*. So a sequence that recurs because a skill
is already teaching it is not independent evidence for proposing that skill, and without a guard the
loop is self-confirming by construction: propose, accept, observe the behaviour the acceptance
caused, propose again with a larger count.

The predicate is one line. What it had to read did not exist. `chemclaw_skill_loads_total{skill}`
says a skill was read and **never in which turn** — a Prometheus counter is not a join key, and
`chemclaw_local_skill_loads_total` is deliberately bare besides. Nothing else in this system recorded
which skills a turn loaded.

Built as specified, the guard would therefore have been a function reading a column nobody writes.
This repository has deleted that shape twice and named it both times: `map_to_hpc_identity`, and
`audit_events.agent` — empty on every row that trail ever wrote, while three docstrings said in the
present tense that it named the agent beside the human
(`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`).

## Decision

**`turn_costs.skills_loaded` is the producer**, added by `infra/sql/105_turn_skills_loaded.sql` on
`082_turn_knowledge.sql`'s pattern: additive, defaulted, computed from something that already
happens on every turn. It is written from the same call each counter is taken on — one condition
rather than two that could drift — and carries **names, not a count**, because the guard asks whether
*this particular* skill was acting.

**Both tiers land in one array.** A personal skill shapes a turn exactly as a reviewed one does, and
a guard blind to the personal tier would be blind to the one most likely to be self-confirming,
since that is the tier the agent can propose into.

**A chemist's own skill name is safe here and not on a metric label**, and the asymmetry is worth
stating because it looks like an inconsistency. `chemclaw_local_skill_loads_total` is bare because a
Prometheus exposition is shared and no erasure reaches it. This is a per-actor row in this system's
own database, erased with that person by `agent/leaver.py`.

**The guard runs at session granularity, and that is stricter than per-turn rather than weaker.** A
skill read on a session's second turn shapes its third and its tenth, so asking "was this skill
loaded in this *turn*" would admit exactly the evidence the guard exists to exclude. The question is
whether the skill was acting in this conversation.

**The guard can only ever remove evidence**, which `tests/test_distiller.py` asserts as a property
rather than an example: `MIN_INDEPENDENT_SESSIONS` is the census's own bar restated on what survives,
so a proposal can be made harder to justify and never easier.

**A distilled proposal is a scaffold and its evidence, not model prose.** The body names the
trajectory, the sessions it recurs in and the count, with the judgment left as an explicit gap —
because a chemist editing a scaffold they can check is a better bargain than prose a model wrote
about work it is summarising from tool names alone. Model-written drafts have a better path already:
`propose_skill`, called in the turn where the reasoning is in context. The scaffold is
**deterministic**, because the queue keys on the content hash: a body carrying a timestamp or a
re-ordered session list would turn one idempotent proposal into a new one every run, and a rejection
would stop meaning anything.

**Both proposers run on demand and are dry by default.** `CLAUDE.md`'s rule is that no Temporal
Schedule opens a pull request and knowledge never arrives on a timer; the campaign and playbook
miners already work that way. Dry-first is deliberate rather than cautious: the output is the
evidence for whether the guard is doing anything, and a miner whose effect precedes its explanation
is one nobody can audit.

**A distilled proposal is filed under the chemist the evidence came from**, read from the sessions
rather than taken as an argument — an operator running `make distill` is not the person the
trajectory belongs to, and a proposal filed under the operator would act on the wrong turns.
Evidence spanning more than one chemist is skipped and said so.

## What this does not claim

**The profile proposer produces a record, not an effect.** A profile is a file in `data/profiles/`,
git-resident and reviewed in a pull request, and no HTTP route can commit. What accepting one buys is
a person reading a rendered YAML and opening that pull request, instead of nobody noticing the
pattern. Saying it plainly is better than a queue implying otherwise.

**It measures co-occurrence, which is a pattern and not a benefit.** Whether a narrower surface
answers *better* is a different question this repository has measured twice and both arms are on
record (`D-2026-08-12`, `D-2026-08-13`). A proposal from here claims the pattern and never the
benefit, and its rationale says which.

**Neither proposer has anything to say about this corpus, and that is the expected first result
rather than a fault.** `make trajectory-census` measures the history at zero, so `make distill`
reports zero and `make propose-profile` finds no cluster. Both say which of the two they are — an
empty database, or a corpus with no recurrence — because a miner that printed nothing would be
indistinguishable from one that was broken.

## What keeps it true

- `tests/test_distiller.py::test_a_trajectory_that_recurs_independently_is_a_candidate` and
  `::test_the_same_trajectory_is_not_a_candidate_where_the_skill_was_already_acting` — **the pair is
  the control.** Identical corpus, identical census, one difference, and they must disagree; a guard
  asserted only where it is a no-op is the shape this ADR is about.
- `::test_the_guard_only_ever_removes_evidence` — as a property, because the failure that matters is
  a guard that *added* a session.
- `::test_a_skill_of_another_name_is_not_self_confirmation` — the superset error, which would make
  every pattern invisible to a chemist who uses one skill heavily.
- `::test_the_bar_is_the_census_s_own_bar_on_the_surviving_evidence`
- `::test_a_scaffold_is_deterministic_and_says_it_is_unfinished` and
  `::test_a_scaffold_is_a_valid_skill_the_queue_will_accept` — a proposal a person accepts must be a
  document that can actually be written.
- `::test_a_miner_cannot_fill_a_queue_past_what_a_person_could_accept`
- `tests/test_skills_loaded_record.py` — the producer, driven from a skill body to the ledger:
  both tiers announce, a failed read and a document beside the tree announce nothing, and the ledger
  folds both tiers into one deduplicated set.
