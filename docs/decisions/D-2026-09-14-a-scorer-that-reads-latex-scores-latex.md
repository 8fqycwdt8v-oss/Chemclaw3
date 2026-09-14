# D-2026-09-14-a-scorer-that-reads-latex-scores-latex — the benchmark's markup, and its third outcome

**Status**: accepted. Extends `D-2026-09-14-a-number-somebody-else-can-produce`'s scorer section;
its two published arms are corrected by `D-2026-09-14-tools-were-never-the-variable`.

## Context

`cli/live_benchmark.py` compares the model's text to the option string, and the vendored corpus
keeps ChemBench's raw markup: **21 of the 100 keys** contain `\ce{}`, `\pu{}` or math mode. A model
answers in the spelling a chemist uses. The benchmark's own ADR already records one version of this
family — a scorer that read the *reasoning* measured itself — and this is the other half of it.

## The finding

**Measured over the whole corpus, with every option answered in the plain spelling a chemist would
type: 88 of the 418 options scored as something other than themselves.** Most booked as
abstentions, which is the column this benchmark's central claim ("it declines twice as often") is
read off. At least one booked as a *different option*:

`materials_science:polymer_chemistry_19`'s key is `\ce{FeSO4} + t-butyl hydroperoxide`. The model's
last line was `FeSO4 + t-butyl hydroperoxide` — correct. The exact matcher missed the mhchem
spelling, fell through to its whole-answer fallback, and credited `Azobisisobutyronitrile`, which
the model had named only to rule it out. **A wrong answer recorded for a right one**, and it moves
the score where an abstention only widens the gap.

**The scorer is not neutral between the arms**, which is why this is not merely noise: the arms do
not write markup equally often, so re-scoring the review's raw answers with a normaliser moved the
default arm +10 and the control +5. A benchmark whose instrument favours one arm cannot report a
difference between them.

## The second finding: `unparsed` was booking failures

`_ask` collected only `type == "answer"`, and `api/runner.py` turns every failure into an
`ErrorEvent` — `loop_cap_reached`, `spend_cap_reached`, a connector outage, a degraded capability.
Each of those arrived as `answer=""` and was recorded as *the model naming no option*, which is a
claim about the model's judgement.

**The arms cannot produce that outcome equally.** The default arm binds every connector and can
fail all of those ways; the toolless control binds none and structurally cannot. So the failure
mode landed on one arm's abstention count only, in the direction that flatters the control — and
nothing was written per question, so the three causes could not be separated after the run.

## Decision

- `_normalised` undoes **typography and nothing else**: the mhchem/`siunitx` wrappers (`\ce`, `\pu`,
  `\text`, `\mathrm`, `\mathit`) lose the command and keep the argument; braces, `$`, `^` and `_`
  go; sub- and superscript digits fold to ASCII; the handful of symbol commands this corpus
  actually uses map to a token their Unicode spelling maps to as well. Applied to **both** sides.
- Symbol commands are **mapped, not deleted**. Deleting `\Delta` folds `ΔH` and `ΔG` toward each
  other, and two options differing only by a symbol are exactly the pair a scorer must keep apart.
- Whitespace in an option becomes `\s*` in the pattern rather than being deleted, so `\pu{53.2 L}`
  answered as `53.2L` matches while the word boundaries the decimal-point guard depends on survive.
- It is deliberately **not** a chemistry parser. Anything that *interprets* a formula would make
  the score a property of this file, which is the objection `_prompt` already records against
  prompt engineering here.
- `Answered.error_code` carries the failed turn's code, `render` reports failures on their own
  line with the codes, and an errored turn is **not** an abstention.

## Consequences

- Every published figure from this benchmark predates the normaliser. Re-scoring the review's raw
  answers read 58 → 68 and 76 → 81; those are the review's numbers, not a re-run — see the
  companion ADR for why this session could not produce one.
- A future run reports three outcomes, and a run with a non-empty failure line is not comparable
  to one without.

## What keeps it true

- `tests/test_live_benchmark.py::test_every_option_is_scorable_in_the_spelling_a_model_would_use` —
  the whole corpus rather than a sample, against an oracle written the other way round from the
  implementation (it spells symbols out as Unicode where `_normalised` folds them onto words), so
  the two meet in the middle instead of agreeing by construction.
- `::test_an_option_answered_verbatim_still_scores_as_itself` — the other direction, because a
  lossy transform applied to both sides can break a comparison it was meant to leave alone.
- `::test_the_markup_defect_credited_a_wrong_option_for_a_right_answer` — the worked example, from
  the run that found it.
- `::test_a_turn_that_failed_is_not_a_turn_that_declined` — three outcomes, and the report says
  which.
