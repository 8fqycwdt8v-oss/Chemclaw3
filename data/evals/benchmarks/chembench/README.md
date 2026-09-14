# ChemBench subset — the first external benchmark this system has been scored on

Everything in `make eval` is first-party: 15 case files, a 7-document retrieval corpus, a 39-note
knowledge graph. It is honest and it is not comparable to anything. This corpus is the other kind of
number — one somebody else can also produce.

**What it is.** 100 questions from [ChemBench](https://huggingface.co/datasets/jablonkagroup/ChemBench)
(MIT, expert-generated), 13 from each of eight categories. Only items whose `target_scores` name
exactly one correct option are kept: every other item type in ChemBench needs a scorer this
repository does not have, and a benchmark half-scored is worse than one not run.

**How it is scored.** `make live-benchmark` asks each question of a running front door and takes the
answer the model gives, matched against the option list. Deterministic — there is no judge, which is
the whole reason a keyed benchmark is worth having beside the direction-graded probe corpus.

**What it is not.** It is not a measure of the *tools*. A multiple-choice chemistry question is
answered from what the model knows; this system's retrieval, its knowledge graph and its
calculations have nothing to add to "what compounds form when aniline reacts with nitrous acid".
Read it as a floor — what the deployment's model brings before this system does anything — and read
`data/evals/probes/` and `make live-ab` for what the tools change.

**There are two control arms and they answer different questions.** `--profile tools-removed`
removes every capability tool and changes nothing else. `--profile no-tools` removes them *and*
replaces the system prompt, because a profile's `instructions:` are a wholesale replacement — so a
difference measured against it is a prompt result. The first published pair here was read the wrong
way round; `D-2026-09-14-tools-were-never-the-variable` has the measurement.

**Contamination.** ChemBench ships a canary GUID precisely because these questions leak into
training corpora. A high score is therefore not automatically a good sign, and the number's value is
comparative — the same subset, two configurations, one afternoon — rather than absolute.

**Refreshing it** is an operator-run fetch outside the serving image, reviewed in a pull request,
with the `sha256` in `dataset.json` re-recorded. The seed and the shape are in the ADR
(`D-2026-09-14-a-number-somebody-else-can-produce`).
