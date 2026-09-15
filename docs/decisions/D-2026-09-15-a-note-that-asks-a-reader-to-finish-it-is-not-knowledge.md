# D-2026-09-15-a-note-that-asks-a-reader-to-finish-it-is-not-knowledge — the miner wrote a to-do into the corpus, and five docstrings described a pipeline that does not exist

**Status:** accepted · **Date:** 2026-09-15

## Context

`memory/jobs._summary` built the body of every cross-project playbook the miner mints, and it ended:

> Distil the transferable rule and conditions from the cited evidence.

Nothing ever did. `skills/playbook-distillation/SKILL.md` exists and is always visible to the model
(it declares no tools, so `agent/skill_access.py` never hides it), but it is loaded on demand in a
chat turn and **no durable path invokes it**: `PlaybookDistillationWorkflow` fans
`build_playbook_notes` out to `publish_memory_note_activity`, which calls `record_note`. There is no
model call anywhere on that path, and no `update_note` tool exists.

So the sentence was an instruction to a reader who never came — written into `knowledge/`, which is
what retrieval reads. **Measured**, on a two-project fixture:

- the excerpt a chemist is shown for the term "recurring" contains the instruction verbatim;
- `Note.headline()` — which `D-2026-09-15-a-digest-that-names-an-id-names-nothing` had just made a
  digest use to announce new knowledge — renders as *"Transformation recurring across 2 projects …
  Distil the…"*. So the two changes compound: a subscriber's digest would announce the system's own
  to-do as a finding.
- the note carries `tags: []`, so nothing could find it to complete even deliberately.

A knowledge note that asks its reader to finish it is worse than no note. An absent playbook is
discovered in one query; this one is discovered by acting on it.

**And the claim was not confined to `_summary`.** Five docstrings across `memory/` said a skill
refines these notes — `campaign.py`, `optimization.py`, `playbook.py`, `jobs.py` and
`progression.py`. All four skills they name exist, so this is not a dangling reference; what is
false is the present tense. Phrased as though something layers the judgment on, they read as a
pipeline. That is the shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted elsewhere in this
tree, where three docstrings described a trail naming the agent while the column was empty on every
row it had ever written.

## Decision

**The body states the finding. The epistemic status is a label the system can count. The docstrings
say what is true.**

- `_summary` states what the miner found — this transformation recurs across these projects, here
  is a representative reaction — and stops. That finding is real, deterministic, and knowledge the
  moment it is made (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`).
- `playbook_note` takes `distilled: bool`, and an undistilled one carries `UNDISTILLED_TAG`. The
  cluster miner passes `distilled=False`; `durable/observation_jobs.py`, which promotes an
  observation whose `statement` *is* a claim, passes nothing. **A tag rather than a note type**: a
  second type would make the pattern invisible to every reader that already asks for playbooks,
  while a tag leaves it in the corpus, citable, and says what it is.
- `UNDISTILLED_TAG` lives in `kg/note.py` beside `KNOWN_NOTE_TYPES`, because two packages must agree
  on the string and only one of them may import the other: `memory/jobs.py` stamps it, `kg/analytics.py`
  counts it, and `kg` is layer 4 with no edge to `memory`.
- `GraphGaps.undistilled_playbook_ids` reports them, through the `find_knowledge_gaps` tool that
  already exists. **This is why there is no new tool**: that tool already *is* the "what should
  synthesis do next" surface, and a field on a return model costs nothing in the prompt prefix —
  which matters directly, since `D-2026-09-15-a-suggestion-is-points-and-a-design-needs-labels`
  had just spent its budget getting a new tool's schema back under the ceiling.
  The two fields answer different questions and neither subsumes the other:
  `tags_without_distillation` asks which *topics* have no playbook, this asks which *playbooks* are
  still waiting, and only the second yields a note id to act on.

## Consequences

- A chemist meeting a mined playbook as evidence reads a finding and a tag, not a to-do.
- "Which recurrences has nobody generalised" becomes answerable. Completing one needs no new write
  path: `record_knowledge_note` with the same id overwrites in place, which is how `kg/record.py`
  already works.
- **The absence test found two more producers than the review did.** Scoping reported three false
  docstrings; the scan reported five. That is the argument for scanning rather than listing.
- **It also caught its own first correction**, which quoted the retired phrase while explaining why
  it was retired. The phrase is therefore banned outright, including in prose about the ban: a scan
  that exempted quotations is one an author defeats with quote marks, and it leaves the next reader
  unable to tell a retired claim being explained from a live one being made.

## What keeps it true

- `tests/test_memory.py::test_a_mined_playbook_states_a_finding_and_asks_the_reader_for_nothing`
- `tests/test_memory.py::test_a_mined_playbook_is_findable_as_undistilled_and_a_promoted_one_is_not`
- `tests/test_memory.py::test_no_producer_claims_a_skill_layers_onto_its_note_automatically`
- `tests/test_knowledge_gaps.py::test_a_playbook_that_records_a_recurrence_and_states_no_rule_is_named`
