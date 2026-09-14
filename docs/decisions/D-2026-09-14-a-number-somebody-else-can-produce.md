# D-2026-09-14-a-number-somebody-else-can-produce — the first external benchmark, and what it found

**Status**: accepted

## Context

`make eval` gates 23 metric values over 15 first-party case files, a 7-document retrieval corpus and
a 39-note knowledge graph, with the science half resting on one solubility value, one BO regret
replay and two mass balances. Every number in it was written here. That is honest and it is
incomparable to anything, and `BACKLOG.md` recorded the consequence: no figure in this repository
could be put beside another system's.

The row was **blocked on a working model credential**. That block is gone: this environment carries
one, and `https://api.anthropic.com/v1/chat/completions` answers as an OpenAI-compatible gateway,
which is exactly the seam `agent/llm_provider.py` already has.

## Decision

`data/evals/benchmarks/chembench/` vendors **100 questions** from
[ChemBench](https://huggingface.co/datasets/jablonkagroup/ChemBench) — MIT, expert-generated — 13
from each of eight categories, keeping only items whose `target_scores` name exactly one correct
option, because every other item type needs a scorer this repository does not have. `dataset.json`
records the licence, the checksum and where a human obtained it, the discipline the sibling fleet
holds every vendored corpus to. `make live-benchmark` asks them of a running front door and scores
by comparison — no judge, which is the whole reason a keyed benchmark is worth having beside the
direction-graded probe corpus.

## What it found, and it is not what the row expected

Run against a real gateway, `claude-haiku-4-5-20251001`, on 2026-09-14:

| arm | correct | named no option | accuracy |
| --- | ---: | ---: | ---: |
| `default` — the full system, every tool bound | 62/100 | **20** | **62%** |
| `no-tools` — the same model, no tools, same questions | 74/100 | 10 | **74%** |

**The system scores 12 points below its own model**, and half the gap is visible in the second
column: it *declines* twice as often. The mechanism is not a defect, it is this system's own
instruction working exactly as written — a closed-book chemistry question has no evidence in any
corpus this deployment holds, and the agent is told not to answer without evidence. Read one of the
refusals and the design is unmistakable: *"this system has no chromatographic model, method store,
or column database… I can't give you one of those three options as though it came from evidence this
system holds."*

That is a **finding about what the benchmark measures**, not a verdict on the system. A
multiple-choice chemistry question is answered from what a model knows; retrieval, the knowledge
graph and the calculators have nothing to add to it. So the honest reading is: this number is a
floor, the control arm is the deployment's model, and the gap is the price of grounding — paid here
on questions where grounding buys nothing.

It also **reproduces `D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter` on independent
data**, which that ADR could not do: the same directional result, measured on 100 expert-written
items nobody in this family chose, with a key instead of a judge.

## The scorer had to be fixed before any of this meant anything

The first scorer scanned the whole answer for an option's text. Measured on five questions against
the real gateway, the same five answers scored **1/5** that way and **4/5** read correctly: a model
that reasons before answering writes every wrong option into its own working, and `"5"` was being
matched as `"1"` and `"4"` as `"2"` inside phrases like *"approximately 1.07%"*. The scorer now
reads the last line first, longest option first, with word boundaries that treat a decimal point as
part of a number and a full stop as a boundary.

**A scorer that reads the reasoning measures itself**, and it is the same family of defect as the
two eval gates this wave opened with — a number that looks like a measurement of the system and is a
measurement of the instrument.

## Consequences

- The benchmark is **not a gate** and is not in `make ci`: a closed-book chemistry score is a
  property of the deployment's model, and blocking a pull request on it would be blocking on
  somebody else's release.
- ChemBench ships a canary GUID because these questions leak into training corpora. The number's
  value is comparative — the same subset, two configurations, one afternoon — not absolute.
- Refreshing the subset is an operator-run fetch outside the serving image, reviewed in a pull
  request, with the checksum re-recorded.

## What keeps it true

- `tests/test_live_benchmark.py::test_the_vendored_corpus_matches_its_recorded_checksum` — a corpus
  that changed under the score is two different things being compared.
- `::test_the_corpus_records_its_licence_and_where_a_human_got_it` and
  `::test_a_corpus_that_does_not_hash_is_refused` — both directions.
- `::test_the_scorer_reads_the_answer_and_not_the_reasoning` — the four cases the live run produced,
  including the two digits-in-prose failures verbatim.
- `::test_the_report_separates_an_abstention_from_a_wrong_answer` — a model that declines is not a
  model that guesses, and on this benchmark that distinction is the entire finding.
