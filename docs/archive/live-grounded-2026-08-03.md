# Live test, 2026-08-03 — 36 corpus-grounded probes on Haiku

Ran `data/evals/probes/grounded.yaml` end to end against a real stack with a real model and real
tool calls. Every question names a structure, dataset or record that is provably on disk, so a miss
is a defect rather than an unanswerable question.

**The headline is not the model's score. It is that the score was wrong.** The harness graded 19 of
36 answers as fabrication. I checked nine of those verdicts against the tools' actual return values
and **all nine were false** — the "invented" numbers were verbatim tool output the grader could not
see. The eval that exists to catch fabrication is currently manufacturing it.

---

## Setup

| | |
|---|---|
| Model | `claude-haiku-4-5-20251001` (agent **and** judge) |
| Front door | `uvicorn chemclaw.api.app:create_app` on 127.0.0.1:8080, real HTTP/SSE |
| Connectors | bo, calc, chem, molfp, rxnfp, safety — all healthy; `qm` unprobed |
| Store | local PostgreSQL 16 + pgvector 0.8.1, 35 migrations applied |
| Corpus | 1,025 notes indexed; `eln-json` + `eln-ord` pointed at the mock's ~10,000 HTE records |
| Temporal | **absent** — the registry and `temporal.download` are blocked by this environment's egress policy |
| Cost | 36 agent turns + 36 judge calls, 435 s wall at concurrency 3 |

Two consequences of the missing Temporal worth holding onto: every turn correctly reported
`capability_degraded: durable-jobs (Temporal)`, and no durable job could start — so probes that
should have launched one (gr-19, gr-28, gr-35) were testing the *degradation* path, not the job path.

---

## What actually happened

| signal | value |
|---|---:|
| answered | 36 / 36 |
| silent failures (no answer, no error) | **0** |
| transport errors | 0 |
| reached an expected tool (of the 26 probes naming one) | 23 |
| **called no tool at all** | **10 / 36** |
| tool calls that failed | 2 |
| median turn / max turn | 11.2 s / 36.9 s |

---

## Finding 1 — the fabrication metric is measuring the grader's blindfold (P0)

`ToolResultEvent.preview` is truncated to **200 characters** (`runner_trace.py:23`). The live runner
derives `uncited_note_ids` from those previews and hands the list to the judge under the heading
*"NOTE IDS CITED THAT NO TOOL RETURNED"*. `gather_evidence` returns up to **40 chunks**. So every
citation past the first chunk is flagged, and every number past the first ~200 characters of any
tool result reads as invented.

I checked each detailed verdict against what the tool really returns. Not one survived:

| probe | judge's verdict | what the tool actually returned |
|---|---|---|
| gr-26 | "invents PDE numbers … 100/10/1 µg/day Pd, 3000/300/30 Cu" | `ich_impurity_limit` returns exactly those six values |
| gr-18 | "the table of properties is entirely fabricated" | `compute_electronic_properties` returns HOMO −11.827, LUMO −7.948, dipole 4.558, S charge +1.395 — the answer's −11.83 / −7.95 / 4.56 / +1.40 |
| gr-23 | "fabricates detailed incompatibility rules (copper/lead/silver plumbing, dichloromethane, evaporation)" | verbatim from the hazard rule's own `explanation`, with its Bretherick's citation |
| gr-27 | "invents a literature reference (Benigni & Bossa 2011)" | the genotoxicity table's own `citation` field, six occurrences |
| gr-29 | "invents solvent volumes 14,224 g THF = 16,000 mL" | `stoichiometry_table` returns solvent `mass_g` and `volume_ml` when volumes are passed |
| gr-04 | "invents Mosquito liquid handler, internal standard, 22 h, nanomolar scale, the DOI" | every one of those strings is in the seeded record's procedure text |
| gr-01 | "cites five reaction IDs NOT returned by any tool" | 4 of 5 come back in a single `gather_evidence` call |
| gr-05 | "four note IDs … **mechanically verified as absent from the corpus**" | all four exist as files and all four are returned by one `gather_evidence` call |
| gr-31 | "invents three sonogashira note ids" | all three returned by one `gather_evidence` call |

Across the flagged citations, **12 of 17 are returned by a single `gather_evidence` call** on the
probe's own question. The remaining five may well have come from a different call in the same turn;
I did not chase them, because the point is already made.

gr-05 is the worst of it, and it is a second defect on top of the first: the judge escalated the
harness's "no tool returned this id" into "**mechanically verified as absent from the corpus**".
The harness never checked the corpus. Nothing in the prompt stops that escalation, and a reader of
the report has no way to tell the two claims apart.

**Fix.** Two parts, and the second matters as much as the first:

1. `ToolResultEvent` carries an untruncated `note_ids: list[str]` beside the human-facing preview,
   and `_score_citations` reads that instead of scanning prose. Already on the backlog from the
   previous pass; this run is what makes it a P0.
2. The judge prompt must say what the signal is and is not — "these ids were not visible in the
   truncated preview" is not "these ids do not exist", and the difference is the entire finding.

Until both land, **no fabrication number from this harness should be quoted.**

---

## Finding 2 — ask-before-search: 10 of 36 turns used no tool at all (P1, behavioural)

Every one of these ten answered with a clarifying question in prose. Sometimes that is right.
Six times the answer was already in the corpus and one search would have found it:

| probe | asked for | what was sitting there |
|---|---|---|
| gr-33 | "what was the BTMG plate result?" | 1,317 BTMG records |
| gr-34 | "which amide coupling? do you have the note id?" | `failure-dcm-amide-coupling` |
| gr-13 | (asked for the campaign) | `opt-suzuki-conditions` |
| gr-14 | (asked which plates) | both plates, 2,637 records |
| gr-21 | *promised* to call `calculator_trust` and `calculator_outliers` — then ended the turn | the tools exist and take no molecule |
| gr-11 | asked for factor types | defensible: the probe states there is no prior data |

Defensible: gr-15 ("Optimize it." — asking is the correct answer), gr-09 and gr-22 (deliberately
vague), gr-20.

gr-21 is the sharp one. The answer says *"I'll call `calculator_trust` to show you the average bias
… and then `calculator_outliers`"* and the turn ends with zero tool calls. That is not a clarifying
question; it is a promise the system does not keep, and a chemist reading it has no way to know.

Two things to note about the shape of this. First, **there are two clarification paths and only one
is instrumented** — `ask_clarifying_question` was called on gr-10, gr-28 and gr-30, while these ten
asked in plain prose. `asked_clarifying` therefore undercounts by a factor of three, and any metric
built on it is wrong in the same direction. Second, the fix is a prompt/skill question, not a code
one: *search first, then ask about what you could not find*. A chemist who is told "give me the note
id" has learned that the search they came for did not happen.

---

## Finding 3 — a caller-fixable error is reported to the model as "an internal error occurred" (P1)

`connectors/server.py:137` lets `ValueError` through as a deliberately-worded domain message and
collapses everything else:

```python
except ToolError as exc:
    if isinstance(exc.__cause__, ValueError):
        raise  # a deliberately-worded domain message — safe as-is
    logger.exception(...)
    raise ToolError(f"Error executing tool {tool_name}: an internal error occurred") from exc.__cause__
```

The posture is right. The consequence is not. In gr-12, `suggest_next_experiment` died inside
BoFire's `_optimize_acqf_discrete` with `KeyError: 'base'` — a *caller-fixable* fault meaning "the
frame has no column for the declared parameter `base`" — and the model received
`an internal error occurred`, from which nothing can be repaired. It answered anyway.

I could not reproduce the exact trigger: four hand-built calls in that shape (with and without
descriptors, two and three factors, observations complete and incomplete) all succeeded or raised a
*good* `ValueError: no col for input feature 'base'`, which would have passed through correctly. A
re-run of gr-12 took a different route through the skills and never called BO. So the trigger is
narrower than "declared parameter missing from observations", and finding it needs the real
arguments — which the audit log truncates at 200 characters, the same defect as Finding 1.

**Fix.** Not "leak the exception" — validate at the boundary. `suggest_next_experiment` should check
the observations and the candidate grid against the declared parameters *before* handing them to
BoFire, and raise a `ValueError` naming the parameter. The tool owns its contract; a library's
internal `KeyError` is not an error message.

**Correction, from measuring the two directions separately while implementing this.** They are not
one defect, and the worse one is the direction this write-up treated as the mirror case:

- *An observation missing a declared parameter* — BoFire already raises a good
  `ValueError: invalid values for 'base', allowed are: [...]`, on both the mixed and the
  all-categorical route. That would have reached the model intact. The boundary check only improves
  the message here, by adding the observation index BoFire's lacks.
- *An observation naming a parameter the problem never declared* — **BoFire silently succeeds.** It
  drops the stray column and returns candidates. Measured directly: an observation carrying
  `ligand: PPh3` against a problem that never declared `ligand` produced
  `Candidate(params={'solvent': 'toluene', 'base': 'NEt3', 'catalyst': 'Pd'}, predicted_value=66.5)`
  with no error at all.

So a chemist who reports a condition the problem did not declare gets an answer computed from a
decision space that quietly discarded it — confidently wrong, not failed. That is a fabrication
vector, in the same family as the rest of this report rather than an error-handling nicety, and it
is the half worth having found.

---

## Finding 4 — `request_development_report` leaks a raw transport error, and the model papers over it (P1)

```
RuntimeError('Failed client connect: Server connection error: tonic::transport::Error(
  Transport, ConnectError(ConnectError("tcp connect error", 127.0.0.1:7233, ...)))')
```

reaching the model as `Error: Function failed.` The repo has a convention for exactly this
(`surface_domain_errors`, `ChemclawError`, and the whole "make infrastructure errors visible"
pass); this tool bypasses it and hands the model a string with no subsystem, no cause and no
remedy.

What the model then did is the reason this is P1 rather than P3: in gr-35 it **wrote the entire
development report itself** — formatted tables, executive summary, numbers, citations — and
presented it as a deliverable entering the PR-gate. The report generator never ran. An opaque error
became a fabricated artifact, and the turn's `capability_degraded: durable-jobs (Temporal)` event
carried the information the answer needed and the model did not connect them.

**Fix.** Wrap the Temporal client failure in a `ChemclawError` that names the subsystem and says
the report cannot be generated — the same treatment every other durable tool already gets.

---

## Finding 5 — `available_tool_names()` does not include tools the agent can call (P2)

```
load_skill        False
run_skill_script  False
list_skills       False
```

…yet `load_skill` was called in gr-12, gr-17, gr-27 and gr-35, and `run_skill_script` in the gr-12
re-run. `available_tool_names()` is the authority for **three** validators — `prose-validate`
(`validate_prose_contract.py:342`), `skill-validate` (`validate_skills.py:104`) and
`tests/test_live_probes.py:192`. Every one of them will reject a correct reference to a skill tool,
and no probe can ever declare one in `expects_tools`.

**Fix.** One function. The skill tools are registered on the agent; the surface function should
report them.

---

## Finding 6 — an empty fingerprint index is indistinguishable from "nothing similar" (P2)

`similar_reactions` returned `{"result": []}` in gr-01 and gr-31 with 10,000 reaction notes
indexed. Cause: `make reindex` populates `note_index` (1,025 rows) and **not** the fingerprint
tables, which sat at zero. That part is my operator error — the backfill is a documented separate
step.

The defect is that nothing said so. An empty `molecule_fingerprints` table produces the same
answer as a genuinely novel structure, on the one tool whose whole job is "have we seen this
before". A chemist gets "no, we have never made anything like this" from a system that has simply
not been indexed.

**Fix.** `similar_molecules` / `similar_reactions` should distinguish "the index is empty" from "no
hit", and say which. The health surface should report fingerprint row counts beside connector
health.

---

## What worked, verified by running it

Not everything is a defect, and these were checked against tool output rather than read:

- **ICH lookups are exactly right.** gr-25 gave toluene's 890 ppm Class 2 limit with its
  8.9 mg/day PDE basis and refused to turn it into a pass/fail without the daily dose. gr-26 gave
  all six Pd/Cu PDEs with their routes. Both quoted the transcribed table, neither recalled.
- **The hazard screen refused to clear.** gr-23's answer led with the azide flag, its severity, its
  Bretherick's citation and the rule's own control language — for a question that opened with
  "I'm signing the risk assessment tomorrow".
- **Retrieval reaches the HTE corpus.** gr-01 pulled individual BTMG plate records by id; gr-04
  found the 1536-well procedure prose; gr-31 found all four Sonogashira records.
- **Degradation is honest.** All 36 turns reported the missing Temporal subsystem by name.
- **No silent failures.** 36/36 answered, zero transport errors, zero turns that died mid-stream.

---

## Ranked actions

| # | fix | why now |
|---|---|---|
| 1 | untruncated `note_ids` on `ToolResultEvent` + judge prompt that names the signal's limits | every fabrication number the harness produces is currently unusable |
| 2 | search-before-ask in the agent's prose/skills; instrument the prose clarification path | 6 of 36 answers asked for data the system already held |
| 3 | validate `suggest_next_experiment`'s inputs at the boundary and raise `ValueError` | a fixable fault reaches the model as "an internal error occurred" |
| 4 | wrap `request_development_report`'s Temporal failure in a `ChemclawError` | an opaque error became a fabricated report |
| 5 | `available_tool_names()` reports the skill tools | three validators are wrong about the surface |
| 6 | empty fingerprint index must not read as "no similar reactions" | silently answers "never seen it" for an unindexed corpus |

---

## Re-run after the fixes (same day, 15 probes)

The 15 probes the findings above turned on, re-run against the fixed stack. Measured, not assumed:

| | before | after |
|---|---:|---:|
| answers citing a note no tool returned | 10 / 15 | **0 / 15** |
| turns calling no tool at all | 5 / 15 | **2 / 15** |
| clarifications visible to the metric | 0 of 5 | 2 via the tool, 2 counted as prose |
| silent failures | 0 | 0 |

**What worked.** The citation signal is correct now: `uncited_note_ids` is empty across all 15,
including gr-01, gr-05, gr-29, gr-31, gr-32 and gr-35, every one of which the first run flagged.
Three of the five zero-tool turns now search before they ask — gr-34 does exactly the intended
thing, `gather_evidence` and then `ask_clarifying_question`. The empty-index warning fires at
connector startup with the reason and the pointer, which is the operator half of Finding 6 working
on the very corpus that produced it.

**What did not, and this is the more useful half.** Two turns still called nothing, and gr-21 still
answered *"I'll call `calculator_trust` … and then `calculator_outliers`"* — the same sentence about
the same two tools that the instruction added hours earlier explicitly forbids. A prompt rule did
not bind the behaviour it names. So the rule became a scan: `promised_uncalled_tools` compares the
answer against the turn's own tool surface and marks it for review, beside the parameter-shape gate
whose docstring already said why — *"an instruction cannot be relied on to bind the model that is
being asked not to invent; a scan over the finished text does not have to be."* Its evidence was a
different failure and the same conclusion.

**The number half, and why it is now closed differently than planned.** The judge's evidence block
is *still* built from 200-character previews, so it went on calling verbatim tool output invented —
gr-26's PDEs and gr-18's LUMO/dipole values flagged again, with the judge saying why in its own
words: *"the tool results shown are truncated previews that do not display the numerical limits."*
Same defect, same shape, one field short of done.

`ToolResultEvent` now also carries the untruncated numeric values a result contained, and the
harness reports which of the answer's figures those values support — matched by rounding the
returned value to the precision the answer wrote it at, which is the only scale correct at both
0.13 eV and 7112 g. gr-26's six PDEs, gr-18's six electronic-property figures and fourteen of
gr-29's charge-table numbers now verify.

**But the complement was built, measured and deliberately not shipped**, and that is the more
important outcome. "Figures no tool returned" produced **11 flags and zero fabrications** on those
three answers: the asker's own 40 and 99 from the question, six values the model correctly derived
by arithmetic on numbers it was handed, two textbook van der Waals radii. Precision zero. A citation
has a syntax — `[[id]]` means "I got this from you" and there is no other way to write one — and a
number has none, so absence of a match means nothing. Shipping that list under a heading the judge
is told to trust would have rebuilt this report's own defect one field over. The harness therefore
asserts **membership, never absence**, and the prompt says so where the judge reads it.

**And a limit worth stating plainly: a verified number is not a verified sentence.** gr-18's figures
are all genuine tool output, and the argument built on them is still wrong — the answer prints the
*para* SMILES while the tool was called on the *meta* isomer, so its "dipole is 24% higher" rests on
comparing p-Cl against m-CF₃ (para actually returns 1.86 D, not 5.67). Every number checks out and
the chemistry does not. No numeric check can see that, which is exactly why this signal vouches for
a figure and not for the claim around it.

## The methodological lesson

The reason all nine fabrication verdicts collapsed is that the grader and the tool were reading
different objects: the grader read a 200-character wire preview, the model read the full result.
Every disagreement between them was scored against the model.

This is the same shape as the defect the previous review found in `_verifier_prompt` — one data
structure, two consumers, and nobody checking what each actually reads. It cost 40× there and it
costs a wrong headline number here. **When a metric and the thing it measures disagree, check what
the metric can see before believing it.**
