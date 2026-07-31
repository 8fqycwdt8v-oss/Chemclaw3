# D-163 — The prose gate learns note types, and the two dead ones it finds

**Context.** Reviewing D-162 turned up a defect it did not introduce but sits directly on top of.
D-162 added `experiment-proposal` to `KNOWN_NOTE_TYPES` as the type an agent writes when it
proposes a run. Checking that against the neighbouring prose showed that the two note types
already being taught **do not exist**:

- `skills/deep-research/SKILL.md` — "draft it as an agent note through `propose_knowledge_note`
  (type `protocol` …)".
- `connectors/bo/skills/experiment-design/SKILL.md` — "draft it through `propose_knowledge_note`
  (type `experiment-batch`)", the phrasing D-024 introduced.
- `skills/knowledge-graph-write/SKILL.md` enumerated both in its list of the kinds a note may be.

Neither is in `KNOWN_NOTE_TYPES`. The failure is quiet and late: `propose_knowledge_note` accepts
any type (deliberately — D-084 keeps the enforcement at `kg-validate` so the agent can *propose* a
new kind), so the call succeeds, the PR-gate opens a branch, and `kg-validate` rejects it **on the
PR the agent just created**. The chemist sees a broken proposal rather than a proposal.

`make prose-validate` exists precisely to catch prose promising what the code cannot do (D-117,
gap IDEA-7) — and it checks tool *names* only. Rule 1 could not see this: the tool was real, only
the artifact it was told to write was not. So the gate's blind spot and the bug are the same
shape, one level down.

**Decision 1 — rule 4: a note type named in agent prose must be a known type.** Same union-of-truth
argument as rule 1, against `KNOWN_NOTE_TYPES` instead of the tool surface. The pattern is
deliberately narrow — the word `type`/`types` followed by a backticked slug — rather than "every
backticked slug in a sentence about types", which in this corpus is mostly tools, frontmatter
fields and chemistry. Narrow means a convention prose has to follow (write it as
``type `x` ``), stated in the validator docstring and in `skills/README.md`, and it means the rule
stays true instead of noisy. That is the same trade rule 2 made when it required an underscore.

**Decision 2 — one type for "run this next", whatever produced it.** `protocol` and
`experiment-batch` both become `experiment-proposal`. They were three names for one artifact — a
set of conditions the agent is suggesting be run, awaiting a human's approval — and the difference
between them was *how it was derived*, which the note body states anyway. A reviewer approving
"run this next" should not have to learn three note kinds for one decision, and a retrieval filter
keyed on the type should not have to know which skill authored it.

`bo-candidate` stays distinct and is now explicitly **not** the agent's to write: it is what a
durable campaign mints for itself in `connectors/bo/knowledge.py` when a round completes. The
distinction is by *minter*, which is checkable, not by provenance-in-prose, which is not.

**Consequences.** `make prose-validate` fails on both dead types before this change and passes
after; `tests/test_prose_contract.py` pins the rule, including that it does not fire on the
backticked `type`/`tag` filter names that appear throughout the same prose. Anyone adding a note
kind now hits the gate in CI rather than a chemist hitting it on a rejected PR.

Not addressed: `propose_knowledge_note`'s own docstring still lists the types with an ellipsis
("compound, reaction, job-result, campaign, playbook, …"), which is a third copy of the same
list, kept in sync by nothing. It is honest as far as it goes — an abbreviated list is not a
false claim, and the gate now covers the prose that gives instructions — but the real fix is for
the model-facing description to be derived from `KNOWN_NOTE_TYPES` rather than restated.
