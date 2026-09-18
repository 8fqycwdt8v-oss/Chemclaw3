# D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool — the queue between a model proposing a behaviour change and that change acting on anybody

**Status:** accepted · **Date:** 2026-09-18

## Context

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` drew the axis this repository now gates on:
a thing needs review when it changes **what the agent does**. Knowledge does not, so it lands in the
graph and is corrected. A skill does — it is injected into the prompt and reshapes every later
answer with no citation trail — and what the code does about that is refuse outright:
`agent/skill_backend.SkillsReadOnlyRefusal` refuses every write verb a turn could reach, on the
shared tree and, since `D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved`, on the
chemist's own tier too.

That refusal left something unanswered rather than settled. **The agent is frequently the party that
has just worked out the procedure worth keeping** — a workup that has now failed the same way twice,
an ordering that matters, a rule for choosing between two methods. Before this, its only channel was
prose in an answer that a chemist had to notice, copy and post to `POST /skills/mine`. That is a
worse control than it looks: what a person approves should be the document they were shown, and a
copy-paste is exactly where a document changes without anybody deciding it did.

The plan had this as two phases — a queue, then proposers. Building the queue alone would have been
a mechanism whose only caller is its own test, which is the `reject_widening` shape this repository
deleted 254 lines for, and the same argument that made
`D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved` ship its tier *with* a writer.
Both of the designed proposers need deployment history that `make trajectory-census` measures at
zero. So the queue ships with the one proposer that needs none.

## Decision

**`propose_skill` writes a proposal; `POST /proposals/{kind}/{name}` decides it.** The tool is an
ordinary in-process tool the model calls when it has worked something out. It writes a row in
`behaviour_proposals` and nothing else: no prompt contains what it wrote, neither skills tier is
touched, and `SkillsReadOnlyRefusal` is unchanged. This is the shape `api/routes/plan.py`,
`api/routes/workflows.py` and `api/routes/skills.py` all have, for one reason — a model must never
be able to authorize its own behaviour change — and `infra/sql/020_plan_approvals.sql` records what
its absence costs: a trail showing "an attributable approval with no human act behind it".

**The key is the content, not the name.** Re-proposing byte-identical text is the same proposal, so
a rejection survives the same text arriving again — the whole of "an unchanged re-proposal cannot
reopen a rejection". A changed body is a different proposal and supersedes an open sibling; it never
supersedes a **decided** one, because that would erase the evidence the first rule exists to keep.
Retired `note_proposals` shipped without the first half and `infra/sql/058_note_proposal_superseded.sql`
records the result: a queue rendering versions nothing would deliver, and one decision then applied
to both.

**Content identity is per actor.** Two chemists may independently be offered the same procedure, and
one declining it must not decide for the other — a proposal is per person, like the tier an accepted
one is written to.

**A decision is final.** Deciding twice reports what stands rather than replacing it, and the
refusal names the way forward: a person who declined something and later wants it writes it through
`POST /skills/mine`, the same act with one fewer indirection and no pretence that the agent proposed
it twice. Letting a decision be overwritten would buy a change of mind at the cost of the property
this table exists for — that a rejection is evidence somebody can find later.

**Accepting writes inside the decision.** A queue that records "accepted" and writes nothing is the
shape where a person believes they changed something and did not, with the record agreeing with
them. So acceptance goes through `save_local_skill`, and through the same `agent_local_skills_max`
refusal `POST /skills/mine` gives — a second door into one bound is a hole in it. On a refusal the
proposal stays **open**, which is recoverable: the person removes one and comes back. A recorded
acceptance that failed to write is not.

**`propose_skill` is state-changing, for the gate rather than for the row.** Writing a row is the
small reason; the real one is that a turn proposing a change to what the agent does is something the
plan gate should see — `compose_workflow`'s standing exactly, where what is gated is the procedure
rather than the row. It also subtracts the tool from every helper's surface by arithmetic, which is
`ask_clarifying_question`'s argument: a helper proposing behaviour changes from a context the
chemist cannot see is worse than a helper that cannot propose at all.

**A proposer can learn what became of its proposal**, and the tool answers three different things
because the three situations call for different next moves — *proposed* (say so in the answer),
*already open* (you are repeating yourself, stop), *already decided* (here is the verdict and the
reason; respond to the reason rather than retrying). Without that, the only strategy available after
a decline is to propose again.

**Validation happens at the proposal, not at the acceptance.** A malformed `SKILL.md` that reached
the queue would be reviewed, accepted, and *then* fail at the write — the worst place to discover
it, because the person has already decided and the failure reads as the system losing their
decision.

## What this deliberately does not do

**`kind` carries `profile` and only `skill` has a destination a route can write.** A profile is a
file in `data/profiles/`, git-resident and reviewed in a pull request, and no HTTP route can commit.
An accepted profile proposal is therefore a **record** that somebody wants one, which a person then
raises as a change. That is smaller than it sounds and it is said here rather than disguised by a
column that implies otherwise.

**Nothing proposes automatically yet.** The distiller and the profile proposer are the next phase and
are blocked on history, not on design.

## What it costs

`propose_skill`'s schema is **462 tokens** of static prefix on every model call, inside the 900-token
single-tool bound. Measured after: the `default` profile's floor is **69,874** against the ceiling of
70,600 that `tests/test_context_floor.py` holds, so nothing was re-baselined and no deployment's
thread allowance moved. The headroom is 726, which is thin by this file's own history — a neighbouring
merge once drifted the floor by 326 on a `Raises:` paragraph — so the next tool added here should
expect to move the ceiling and say by how much.

`behaviour_proposals` is refused by `durable/retention.py` and kept by `agent/leaver.py::_RETAINED`,
for `plan_approvals`' reason one layer up: it records who decided what this system was allowed to
become. **It retains more than the row beside it and that is stated rather than inherited** — a plan
approval keeps a hash and a verdict, while a proposal keeps `content`, a whole document a model wrote
about one person's chemistry, held after they leave. The justification is real (a rejection is only
evidence if the text it rejected is still there, which is `note_proposals`' own argument for keeping
the body verbatim) and it is a larger claim, so the erasure report prints it with a count rather than
letting it ride on the neighbour's sentence.

## What keeps it true

- `tests/test_behaviour_proposals.py` — the four rules, driven against **both** backends from one
  parametrised body, so a rule cannot be added to one and forgotten in the other. Isolation is a
  fresh actor per test rather than a truncation, because the table has no DELETE grant by design —
  the same per-person identity rule 4 asserts, spent rather than worked around.
- `::test_an_unchanged_re_proposal_cannot_reopen_a_rejection`
- `::test_a_changed_body_supersedes_an_open_sibling_and_never_a_decided_one` — both halves.
- `::test_a_decision_is_final`, `::test_a_proposal_is_one_persons`
- `::test_the_backend_follows_the_session_store`
- `tests/test_api_proposals.py::test_no_tool_can_decide_a_proposal` — over the registry, not by
  reading the routes.
- `::test_a_decision_binds_to_the_document_the_person_was_shown` — including the supersede-between-
  read-and-click case.
- `::test_accepting_writes_what_was_accepted` — read through the store *and* the route, because
  patching one and reading the other is how an acceptance looks successful while writing nothing.
- `::test_the_personal_tiers_cap_holds_on_this_door_too` — and the proposal stays open.
- `::test_one_chemists_queue_is_invisible_to_another`,
  `::test_a_decision_is_not_replaced_by_a_second_one`,
  `::test_a_deployment_that_keeps_no_store_refuses_the_acceptance_rather_than_recording_it`
- `tests/test_proposal_tools.py::test_proposing_is_state_changing_so_no_helper_holds_it`
- `::test_no_turn_can_write_a_skill_even_now_that_it_can_propose_one`
- `::test_the_model_is_told_which_of_three_things_happened`
- `::test_a_body_that_could_not_be_written_is_refused_at_the_proposal`
- `tests/test_authz.py` — the partition, which fails if a tool is added without classifying it.
- `tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer` — the counter has a panel.
