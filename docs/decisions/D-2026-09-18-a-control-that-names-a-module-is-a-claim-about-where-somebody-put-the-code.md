# D-2026-09-18-a-control-that-names-a-module-is-a-claim-about-where-somebody-put-the-code — what four fresh-context reviews of the evolving-skills work found

**Status:** accepted · **Date:** 2026-09-18

## Context

Five commits shipped the evolving-skills work — the per-skill load counter and `AgentProfile.
skill_names`, the helper roster, the chemist's own skills tier, the behaviour-proposal queue with
its two miners, and a test re-calibration. Each was reviewed at the time. This record is what four
fresh-context reviews found afterwards, two of them driving **mutations** against the merged tree
rather than reading it, and the finding that names this file is the one that generalises: **several
of the controls those commits shipped were assertions about a file's text rather than about the
system's behaviour, and each of them survived the exact defect it was named after.**

Nothing here reopens a decision. `D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved`,
`D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool`,
`D-2026-09-18-a-guard-with-nothing-to-read-is-not-a-guard` and
`D-2026-09-16-a-roster-varies-the-two-dimensions-that-carry-no-authority` all stand. What changes is
what holds them.

## Decision

### 1. An absence test's subject is the registry, not one module's source

`tests/test_proposal_tools.py::test_no_turn_can_write_a_skill_even_now_that_it_can_propose_one` read
`inspect.getsource(proposal_tools)` and asserted `"save_local_skill" not in source`. Driven: a
registered `settle_proposal` tool **appended to that same module** — looking its caller's proposal
up, writing it into their skills tier through a name built at run time, and calling
`decide(accepted=True)` — left that file and `tests/test_api_proposals.py` at **16 passed**. That is
the whole feature's premise ("a turn proposes and a person decides") defeated by the control named
after it.

`tests/test_api_proposals.py::test_no_tool_can_decide_a_proposal` was the same shape one level out:
it matched registered tool *names* against the substrings `accept` and `approve_skill`, so
`settle_proposal` was invisible to it.

The control is now over every module that defines a registered tool, by AST: no such module may name
`save_local_skill`, `delete_local_skill` or `decide`, nor import `local_skills`,
`behaviour_proposals` or `importlib` without an argued entry beside the test. `importlib` is refused
outright there because a module name built at run time is the one thing a static reader cannot
follow, and no tool module has a reason to build one. A second test resolves the three forbidden
names against the modules that define them, so a rename reds the guard instead of satisfying it.

Both mutations now fail — the one in `proposal_tools` and the same one placed in `memory_tools`.

### 2. A blanket suppression written for one signal suppressed the union, and the guard failed open

`_resume_on_job_results` and `_revise_answer` both passed `on_signal=lambda _signal: None`. The
argument for it is `JobSignal`'s alone and is in both docstrings: a resume that fed its own job ids
back into `started_jobs` would let one chemist turn chain durable jobs indefinitely inside one
request. Nothing about a second graph run makes a *skill read* untrue.

`answer_review_max_rounds` ships at **2**, so this was on by default. Measured on identical scripted
streams: the main pass recorded `skills_loaded = ['cold-quench']` and the revision round recorded
`[]`. `agent/distiller.py::independent_sessions` then counts such a session as **independent**
evidence for proposing the skill that was acting in it — the self-confirmation guard failing in the
one direction it exists to close.

`_TurnLedger.note_signal_without_job_chaining` drops that one type and keeps the rest, and an
absence test refuses `on_signal=lambda` anywhere in `api/runner.py`, because the defect is spelled
as a plausible-looking no-op.

### 3. A guard's input needs a test that reaches the row, not the ledger

Two independent one-line mutations — dropping `"skills_loaded"` from `turn_cost_store._COLUMNS`, and
booking `skills_loaded=[]` instead of the ledger's set — left **467 tests passing**. The whole suite
stopped at the in-process `_TurnLedger`, which is the `map_to_hpc_identity` shape
`D-2026-09-18-a-guard-with-nothing-to-read-is-not-a-guard` invokes, happening one layer past where
that ADR's own tests stopped. `tests/test_skills_loaded_record.py` now drives `_book_turn_spend`
over the real Postgres sink and reads it back through the distiller's own query; both mutations red.

### 4. Two miner CLIs and the join between them had no test at all

`cli/distill.py` (151 lines), `cli/propose_profile.py` (190) and `distiller.propose` were reachable
only from the `Makefile`. Replacing `propose`'s store call with an undefined name left 27 green;
filing every proposal under `actor=""` left 27 green. `tests/test_miner_clis.py` and three tests in
`tests/test_distiller.py` close it, including the two fields nothing asserted —
`Candidate.self_confirming` (the guard's actual *finding*) and `bounded`'s primary sort key, whose
old fixture held session count constant at 3.

### 5. A predicted surface that re-derives through the functions it predicts agrees with itself

`test_the_predicted_surface_is_what_a_compiled_helper_binds` calls `predicted_helper_surface`, which
calls `helper_profile` and `helper_connectors` — the same two functions the build calls. So
`helper_connectors` returning its caller's whole reading surface instead of the specialist
intersection left `tests/test_subagents.py` at 59 passed, and no other file imports it. That is the
defect `tests/test_context_floor.py`'s docstring names ("a basis that is re-derived rather than
observed will agree with itself forever") happening to the test that cites it. The intersection is
now asserted against literals.

Beside it: `test_every_roster_description_names_the_surface_its_graph_bound` asserted only `bound ⊆
described`, so a description listing `bound | profile.tool_names` stayed green — which is precisely
the failure `D-2026-09-16` names, a helper advertised as computing that cannot compute. The other
direction is asserted now.

### 6. A tool whose only outcome is unreachable is not bound

`agent_memory_enabled` defaults to **False** and no Helm value sets it, so in every shipped
configuration `turn_store()` is `None`: `POST /skills/mine` and the accept route both answer 503.
`propose_skill` was gated by nothing. Measured under the shipped defaults: it is registered, it
succeeds, it writes a row into a store that dies with the process, and it tells the chemist to go
accept something no route can accept — for **462 tokens** of prefix on every model call.

`local_skills.personal_skills_available()` is the one predicate all three surfaces now ask
(`turn_store`, the builder, and `make distill --propose`, which refuses up front with a non-zero
exit rather than filing rows nobody can drain). Bound both ways: measured, `propose_skill` is absent
from the compiled graph's `ToolNode` with the tier off and present with it on.

### 7. `skill_names: []` means no skills, and the control arm reached one tier

`data/evals/profiles/skills-removed.yaml` exists to be the arm that varies skills and nothing else,
and its own header warns that "a control arm that overstates itself is worse than none". The
personal tier is mounted by the backend rather than selected by `skill_names`, on an argument in
`local_skills.py` that is right about a *named subset* and wrong about the empty set, which is a
profile author writing down that this agent reaches no skill at all. Measured on a compiled graph
with a capturing model: `[skills-removed] personal skill listed: True | shared skill listed: False`.
Every A/B that arm reported still carried personal judgment for any actor with a populated `/mine`.
It now lists neither.

### 8. A ratchet blind to its input is not where a bound belongs

`describe_helper` enumerates the tools a rostered helper's compiled graph bound — the right
derivation, and it made `task`'s own schema a function of how many tools the *sibling fleet* serves.
`tests/test_context_floor.py` binds no fleet connector, so it read `task` at **897** against its
900-token per-tool bound while a deployment serving `safety` would send ~1,009, with the ratchet
passing. That is `D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` one
level down, in the per-tool bound rather than the total, and the remedy that test names ("narrow the
arguments or paginate") does not exist for a description. `agent_helper_menu_tools` bounds the
enumeration at twelve names plus an honest count of what was not listed.

### 9. A frontmatter defect must not widen what a skill is scoped to

`declared_tools` validated the whole `SkillManifest`, and `ToolScopedSkills` reads a missing entry as
"declares nothing" — which leaves the skill **visible to every caller**. So once
`MAX_SKILL_DESCRIPTION_CHARS` arrived, a site skill one character over the limit went from "scoped,
description truncated by the loader" to "unscoped", with a WARNING and nothing else. A read error was
a *widening*, in a filter whose whole contract is one-way. `_declared_pair` reads the two keys the
scoping needs and nothing else.

### 10. An index justified by a query nobody wrote

`105_turn_skills_loaded.sql` created a GIN index for "a containment test over a per-actor slice".
That query was never written: the column's one reader unions the whole corpus in a single pass, and
`EXPLAIN` sequential-scans either form. `106_drop_the_index_nobodys_query_uses.sql` drops it. Same
shape as the paragraph above and as §1 — a structure whose justification is a caller that does not
exist.

### 11. Numbers in prose, again

`CLAUDE.md` said the unnamed helper holds **18** in-process tools and its compiled graph binds
**24**. Measured: **21** and **27**. The 18 was written *in* the commit that shipped the roster and
was already wrong at the next merge, in the paragraph whose subject is why both numbers are given.
They are corrected rather than deleted because the paragraph is about the two *bases*; what is
checkable is the strict-subset inequality `tests/test_subagents.py` asserts.

`tests/test_molfp.py` claimed "nothing between 0.27 and 0.55 corresponds to a behaviour this scan can
have" and that the new bar left "the same headroom a quarter gave". Both false: re-driving that
file's own mutation table on a second machine, the 8-of-16 leak measures **0.511 / 0.566 / 0.562**,
inside the interval declared empty. Three of three still fail, so the control caught the leak every
time — what is gone is the claim that it does so with room to spare. Its failure message also said a
half-leak "reads ~0.75" against its own table's 0.554.

One more, left uncorrected on purpose: `D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved`
says a maximal personal tier costs **5,571** tokens of prefix and it measures **5,575** on the same
fixture. A merged ADR is never edited, the allowance it is charged against is 5,700 so the test
still holds, and the difference is four tokens — recorded here because the alternative is a reader
re-measuring it and wondering which of us was wrong.

## Consequences

- Nine controls that passed their own defect now fail it, each driven rather than argued.
- One shipped behaviour changes: `propose_skill` is not bound where the personal tier is off, and
  `make distill --propose` exits non-zero there. Both are the honest reading of a capability whose
  only outcome is a 503.
- `data/evals/profiles/skills-removed.yaml` is a clean control for the first time; any A/B run
  against it before this carried personal judgment for actors with a populated `/mine`.
- `task`'s description is bounded by configuration rather than by a ratchet that cannot see the
  fleet, which costs a rostered helper's menu its tail beyond twelve names.
- Two numbers in `CLAUDE.md` and two claims in `tests/test_molfp.py` are corrected.

**What this does not settle.** Whether delegation pays is still open, and
`evals/delegation.py` has still never run against a model. Nothing here is evidence about that.

## What keeps it true

- `tests/test_proposal_tools.py::test_no_turn_can_write_a_skill_even_now_that_it_can_propose_one`
  and `::test_the_symbols_that_absence_test_names_still_exist` — §1, over every tool-defining
  module, with the forbidden names resolved so a rename reds rather than satisfies.
- `tests/test_skills_loaded_record.py::test_a_revision_round_still_records_what_it_loaded` and
  `::test_no_graph_run_of_a_turn_suppresses_the_whole_signal_union` — §2.
- `tests/test_skills_loaded_record.py::test_the_row_really_carries_what_the_ledger_folded` — §3,
  through the real sink and the distiller's own query.
- `tests/test_miner_clis.py` and `tests/test_distiller.py::test_a_distilled_candidate_really_reaches_the_queue`,
  `::test_the_guard_s_finding_is_carried_and_not_only_counted`,
  `::test_the_strongest_candidate_is_the_one_with_the_most_independent_sessions` — §4.
- `tests/test_subagents.py::test_a_rostered_helpers_connectors_are_the_specialists_and_not_its_callers`
  and `::test_a_roster_description_names_the_bound_surface_and_nothing_beside_it` — §5.
- `tests/test_behaviour_proposals.py::test_the_counter_distinguishes_the_three_arrivals_it_declares`
  — the queue's three outcomes reach the exposition, which nothing held.
- `tests/test_skill_access.py::test_the_control_arm_that_removes_skills_removes_both_tiers` — §7.
- `tests/test_subagents.py::test_a_roster_entry_s_menu_is_bounded_by_what_it_lists_not_by_what_it_binds`
  — §8.
- `tests/test_skill_manifest.py::test_a_frontmatter_defect_cannot_widen_what_a_skill_is_scoped_to`
  and `::test_a_skill_with_no_readable_name_is_still_undeclared` — §9.
