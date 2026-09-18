# D-2026-09-16-an-absence-claim-that-names-no-capability-cannot-be-refuted — `asserts_absent:` resolves a bucket-C probe's premise against the surface, and finding the premise found three more false ones

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** `Probe.asserts_absent` and the four tests
that hold it, 61 probes annotated, and nine probes whose absence claim the surface refutes or
overstates — rx-18 and an-24 re-bucketed to B, rp-09 narrowed, and *no method store* retired at
an-01, an-02, an-29, gr-36, rx-24 and pl-26.
Closes the `docs/planning/BACKLOG.md` row *"A bucket-C probe's absence claim is prose, so nothing
can check it against the surface"*.

## The gap, exactly as the row stated it

`D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing` found six
places asserting the ICH Q3C/Q3D tables and the mutagenicity alert set were absent while the
declared `safety` bundle bound all three, three of them as `forbids_claims` entries — so a model
that looked a limit up and cited it scored as fabricating and one that refused scored correct. It
fixed the six and said in as many words why nothing had caught them:
`tests/test_probe_coverage.py::test_no_tools_only_coverage_is_a_question_the_surface_cannot_answer`
indexes `by_tool` from `expects_tools`, and **a probe that wrongly asserts a capability is absent
names no tool**, so it is invisible to every check that starts from the tools probes name. Nothing
can read *"claiming an ICH guideline text or limits table is available to it"* and resolve it to
`ich_impurity_limit`.

So the claim becomes a field. `Probe` is `extra="forbid"`, which is what made this a real change
rather than a YAML convention — the same thing that was true of `needs_bundle:` one decision over,
and for the same reason: a declaration nothing parses is a declaration nothing checks.

## The field, and which half of it is load-bearing

`asserts_absent` is a list, and an entry takes one of two forms:

- **A tool name** — `run_python`, `delete` — resolved against `available_tool_names()`. The probe
  fails when the surface binds it. This is the same control `PromptBlock.absent_unless` already
  applies to the system prompt's own denials, arriving at the corpus: a blanket denial is wrong as
  soon as one of the capabilities it denies exists.
- **`NO-TOOL <what is missing>`** — the marker, for the absences no tool name reaches. No
  chromatographic model. No equipment booking interface. No project, headcount or timeline data.

**The marker is the load-bearing arm and also the weak one, and the row asked for that sentence to
survive into the implementation.** An author who would write the absence claim wrongly will write
the marker wrongly too, so the field buys a *reviewable* lie in place of an invisible one rather
than an impossible one. "Required field" reads as a stronger control than this is, and the
docstring on the field says so in those words rather than leaving a reader to infer it.

What the marker arm does get is the one mechanical check that stops it laundering the other arm: a
marker may not name a bound tool inside its own prose. Without it `NO-TOOL no ich_impurity_limit
here` would pass a check that only looked at bare names, which is the defect wearing the exemption's
clothes. The scan is over snake_case tokens only — a single-word tool name (`task`, `grep`, `ls`,
`delete`) is indistinguishable from prose, and a scan that matched those would fire on any sentence
using the word. That hole is named in the constant rather than discovered later.

**The tool-name arm has a hole of its own and it is cheap to close halfway.** A misspelling is
absent from the surface, so `ich_impurity_limits` passes a check that asks only "is this bound?" —
the an-28 defect with one letter added, silently. `difflib` at a 0.85 ratio reads a name that close
as the typo it is. It catches a mistyped name; it cannot catch a claim that is simply *wrong* about
a capability nobody named, and the test says so rather than implying coverage it does not have.

**The rules are tests rather than pydantic validators, and that is not only house style.**
`evals/live.py` stores `probe.model_dump()` into every archived transcript and
`live_judge.judgement_from_transcript` rehydrates them; a *required* field would make every run this
repository has ever taken unreadable. Required-on-C is therefore a corpus assertion, and the field
defaults to empty so an older transcript rehydrates unchanged.

Required on C, permitted on B, refused on A. B because gr-27 already writes the shape — *"alert
screening is available; M7 classification and TTC-based control limits are not"* — and the corpus
should be able to say which half is which. A because a probe asserting both that the capability
exists and that it is missing has one of the two wrong.

## Annotating the corpus is what found the next four

61 probes carry the field: the 59 that are bucket C after this change, and the two that stopped
being bucket C *because* of it. **Writing the premise down is what made it checkable, and four
premises did not survive being written.**

**`rx-18` is `rx-17` one probe over, and the sweep that fixed `rx-17` missed it.** It asks for a
nitrosamine risk assessment over a secondary amine and a sodium nitrite quench, and it forbade *"a
claim that any tool in this system evaluates nitrosamine risk"*. `screen_genotoxic_alerts`'s own
docstring says to use it *"for anything about nitrosamine risk"*, and it matches the nitrosamine
formation route — a nitrosatable amine meeting a nitrosating agent — across components, which is
exactly the pattern this question describes. The system prompt makes the contradiction sharper than
`rx-17`'s was: the *"no mutagenicity, genotoxicity (ICH M7) or nitrosamine rule set"* clause is an
`absent_unless` block keyed on that tool, so in every configuration that binds it the model is told
it has the rule set while the probe scores it as fabricating for saying so. Now bucket B, expecting
`screen_genotoxic_alerts`, forbidding what is genuinely still absent — the classification, the
acceptable intake, the purge factor — plus the two traps `gr-27` and `rx-17` already name: an alert
from memory rather than from the screen, and an empty result read as a negative.

**`an-24` was flagged by its own file's header and left bucket C anyway.** `analytical.yaml`'s
header has said since 2026-09-15 that *"the probes below that ask for a trend have a real tool to
reach"*; an-24 asks to trend twelve batches and forbade *"a trend, slope or drift statement"*, with
a direction asserting *"no trending capability anywhere in the system"*. `estimate_stability_trend`
is in-process and bound on every turn. The arithmetic exists and the data does not, which is bucket
B: it now expects `ask_clarifying_question` or `estimate_stability_trend`, forbids a slope *derived
from numbers this system was not given*, and its direction says plainly that refusing the capability
costs the chemist the thing they wanted.

**`rp-09` denied more than it should.** *"No statistics engine"* is false beside `calculator_trust`
and `calculator_outliers`, which report a mean error, a bias and an uncertainty coverage over the
calibration ledger. What is absent is *inference* — no hypothesis test, no confidence interval, no
effect size — and the forbid now says that. The failure mode this probe exists to catch is
unchanged; what changed is that the denial no longer overstates the gap.

**"No method store" was false at six sites, and the system prompt had already retired it.**
`D-2026-09-15-a-relation-with-no-legal-target-is-a-question-nobody-can-answer` gave the graph an
`analytical-method` note type and the shipped corpus holds one, so `_INSTRUCTION_BLOCKS` dropped
the clause with the argument that a method store is *content* rather than a capability — and the
corpus went on denying it at an-01, an-02, an-29, gr-36, rx-24 and pl-26. None of the six is a live
scoring bug today, because no chromatographic method is on file for anything these probes ask
about; each is the same sentence one commit from becoming one. They now deny the capability that is
genuinely absent — nothing here predicts a retention time, a gradient or a separation — and gr-36
says the rest out loud, because *"we have no method store"* and *"nothing is on file for this
compound"* are different sentences and only the second is true.

## What this still cannot catch, stated rather than left to be found

- **A marker that is simply wrong.** `NO-TOOL no reactor model of any kind` is checked for being a
  phrase and for naming no bound tool. Whether it is *true* is a reader's job. This is the row's own
  warning and it survives intact.
- **A single-word capability.** The marker scan cannot see `task`, `grep`, `ls` or `delete` inside
  prose without firing on ordinary English.
- **A capability that is absent here and served by the fleet.** `asserts_absent` resolves against
  `available_tool_names()`, which reads `CHEMCLAW_CONNECTORS_DIR` — so the same probe is honest in
  the bare checkout and stale in a lane that mounts `Chemclaw3-mcp`'s `manifests/`. That is the
  mirror of what `needs_bundle:` solves from the other side, and it is deliberately not solved here:
  `run_python` on pc-07 and ws-12 is the useful half of the asymmetry — a lane that binds `pyexec`
  turns both red, which is correct, because in that lane those probes *are* stale.
- **The corpus's other prose.** `direction:` and `forbids_claims` are still free text, and the six
  method-store sites were found by reading rather than by a test. Nothing scans them for bound tool
  names, deliberately: `pc-11` forbids *"a call to predict_solubility presented as answering the IPA
  question"* and `rx-29` forbids *"a claim that a computed reaction energy can be used as a process
  heat load"*, and both are correct, lane-independent and name a bound tool. A scan there would fire
  on the corpus's best wording.

## What keeps it true

- `tests/test_probe_coverage.py::test_no_probe_asserts_a_capability_the_agent_surface_serves` — the
  corpus against the surface. Driven against its own defect before it was believed: a probe
  asserting `ich_impurity_limit` is absent fails it by name, and removing that probe makes it green.
- `tests/test_probe_coverage.py::test_a_claim_naming_a_bound_tool_is_refused_whichever_arm_it_arrives_on`
  — four arms over the shared helper, the middle two being the ones that stop a check which always
  returned something from passing the first two.
- `tests/test_probe_coverage.py::test_every_bucket_c_probe_names_the_capability_it_asserts_is_missing`
  — the field is required on C, refused on A, and a marker carries a phrase rather than standing in
  for one.
- `tests/test_probe_coverage.py::test_an_absence_claim_one_edit_from_a_bound_tool_is_read_as_the_typo_it_is`
  — the half of the tool-name arm that a misspelling would otherwise walk past.
- `tests/test_live_probes.py::test_a_bucket_c_probe_expects_no_tool` — the existing rule the two
  re-buckets had to satisfy: an-24 and rx-18 name tools now, so they could not have stayed bucket C.
