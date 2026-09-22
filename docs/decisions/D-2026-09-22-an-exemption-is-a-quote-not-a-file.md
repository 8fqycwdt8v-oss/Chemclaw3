# D-2026-09-22-an-exemption-is-a-quote-not-a-file — widening the model-facing prose guards

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Closes the `BACKLOG.md` row *"The
model-facing prose guards scan the in-process registry and four bundles, not the surface"*.

## Context

Two guards in `tests/test_prose_contract.py` refuse text that tells the model about something this
system removed: `D-2026-08-26-semiempirical-is-the-whole-tier`'s DFT/HPC tier, and
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`'s PR-gate over agent-written knowledge. Both
read one universe — the in-process tool registry plus the bundles' served tool modules — and the row
found three classes of shipped text outside it: the durable jobs' assembled docstrings, the
`SKILL.md` files, and the system prompt's own `_INSTRUCTION_BLOCKS`. The `results` bundle serves no
tool module at all, so its entire model-facing surface was outside.

The row called the universe and the patterns **one problem**, and it was right: shipped prose in the
added classes names the removed tier and the removed gate *in order to say they are gone*, so
widening with the patterns as they stood would refuse correct text. It asked for the false-positive
rate to be measured before anything was built, the way
`D-2026-09-11-the-debt-was-in-the-claims-not-in-the-code` measured 82.9% and declined.

**Measured.** Widening the universe — the durable jobs' assembled docstrings, every `SKILL.md` a
turn can load, the system prompt's own blocks, the deployment profiles' prose, the template
launchers — the three patterns as they stood produce **ten occurrences across eight texts, and
every one of them is correct text**. A 100% false-positive rate, which is what the row predicted
and why it is a row rather than a widening.

**The row named three classes and there were five.** It missed `_SAFETY_BLOCKS`, which
`instructions_for` appends to *every* profile — so for a profile turn, which skips
`_INSTRUCTION_BLOCKS` entirely, the universe held none of the system prompt — and it missed the
profiles' own `instructions:`, which `agent/profiles.py` says **is** the system prompt. Both were
found by a review of the first version of this change, which had widened to the row's three and
then written "every text this system ships to a model" over the docstring. That sentence is now
followed by the list of what is still outside.

## Decision

**The exemption is a quoted span, not a file, and it is reached only after the patterns have been
narrowed on evidence.** Four choices:

- **The shape the row proposed is disqualified by the one true positive this file has.** It
  suggested "sentence-level with a negation/past-tense exclusion". Driven against the original
  defect — `get_durable_job_status`'s *"**used to** degrade to a bare status, because the DFT job
  returned its own typed result and had its own status tool"* — that exclusion matches it and turns
  the guard green over the exact text it was written to catch. History about a removed tier is
  *always* past tense; that is what makes it history. The tense cannot be the discriminator, and a
  measurement of the false-positive rate alone would never have shown this: it is a recall failure,
  and the corpus of true positives has one member.

- **Two narrowings that are correct by construction, not concessions — and a review corrected both
  of their numbers.** `conceptual[- ]DFT` leaves the tier pattern because it names a *shipped*
  panel — `publish/properties.py`'s `conceptual_dft` group, projected by `publish/project.py` from
  GFN2-xTB frontier orbitals, reaching no DFT tier. It appears in **one** text tree-wide, not the
  three this ADR first claimed, so the carve-out saves one exemption; it is kept as a carve-out
  because it is a rule about a word's meaning rather than a judgement about one sentence. And
  `propose[sd]? (?:what|it|them|the)` is dropped: over the widened universe it produces **six
  occurrences across five texts**, of which four propose an *experiment* rather than a review —
  the behaviour `D-2026-08-26` asks for in as many words. Against the three shipped
  `synthesize_memory` defects it catches **one**, not the zero first claimed, and all three are
  independently caught by `for review` and `pull requests?`, which is what makes the drop lossless
  on those strings.

- **The drop is not lossless on the defect's *shape*, and that is stated rather than implied.**
  Rename the review object and the original walks through: "propose what it finds to a reviewer,
  who accepts or rejects it" passes every pattern here. Driven, the obvious widening —
  `reviewers?`, `approve[sd]?`, `sign-off`, `accepts or rejects` — produces **fourteen occurrences
  over nine texts**, at least twelve of them correct, several saying that *nobody* approves and
  three describing the workflow approval that genuinely exists. That is the false-positive shape
  `D-2026-09-11-the-debt-was-in-the-claims-not-in-the-code` measured and declined, so this stays a
  keyword guard with a recall limit written down.

- **The recall widening the row named is free.** `PRs`, `review queue`, `knowledge gate`, `awaits
  review` cost **zero** new hits over the widened universe, so the phrasings a future writer would
  reach for are closed at no cost to the false-positive rate.

- **Three exemptions remain, each a literal quote keyed to the one text it belongs to and required
  to end at a clause boundary.** The first spelling was neither, and a review broke it: it
  flattened whitespace and then matched a quote anywhere in any text, so *"...never present one as
  if it were\nDFT runs are dispatched to the cluster queue..."* assembled an exempted quote out of
  words that were never one sentence and exempted the offence that followed. Keying by text takes
  an exemption's reach from a hundred and eighty-seven texts to one; requiring the next non-space
  character not to be a word character is what refuses the accidental span. Both are driven by
  `test_an_exemption_cannot_form_across_a_line_break` and
  `test_an_exemption_reaches_only_the_text_it_names`.

## Consequences

**Two guards keep the exemptions honest, because an allowlist is the easiest control to leave
behind.** `test_every_exemption_still_quotes_shipped_prose` fails the day one of these sentences is
reworded or its text renamed; `test_every_exemption_is_needed` removes each entry in turn and
requires the text to red without it, so an exemption cannot become decoration.

**`_A_LIVE_GATE` is deleted, and it is the case that pair was written for.** Narrowing
`_A_REVIEW_PROMISE` to require the review object made `propose_skill`'s docstring stop matching at
all, so the map exempted nothing and deleting it entirely was a no-op — an exemption with
authority over nothing, which is the shape this file exists to find, in this file. A review found
it; the per-entry removal check above is what would have. Its citation test goes with it, and with
it a `uv run pytest --collect-only` subprocess per entry.

**The universe is driven, not asserted — and "silent" has two meanings.**
`test_a_forbidden_sentence_in_any_of_them_is_caught` poisons each loader in turn with each
forbidden sentence and requires the guard to red *on the poison alone*, with a control call first.
That catches a class that stops being *called*. It does not catch one that returns *less*: a review
broke the repository-skills half of one loader, dropping 36 of 43 skill texts, and every poison
parametrisation stayed green, because the poison is injected whether or not the loader still
returns anything. So `test_the_universe_reaches_every_class_the_model_reads` now counts each class
against the tree — `SKILL.md` files on disk, profiles in `data/profiles/`, blocks in each prompt
group — rather than only asserting it is non-empty.

**No count is written in the docstring any more.** The one that was there said 31 registered tools
against a surface of 114 and both had moved.

**What is still outside the universe, said here so the next widening starts from it.** Prose
assembled as string constants in modules other than `agent/chemclaw_agent.py`: fifteen modules
under `src/chemclaw` hold some, and `durable/hypothesis_tournament.py` holds a live instance of
exactly the exempted kind ("There is no DFT and no cluster here"). It is not enumerable the way the
six classes are — there is no decorator, no manifest and no directory that says "this string is
sent to a model" — and `docs/planning/BACKLOG.md` carries the row with that as its first question.

**Revisit when:** a shipped sentence needs a **fifth** exemption. Three entries is small enough to
read and the fourth is already foreseeable — the tournament prompt above, the day that class is
scanned. A list that keeps growing after that is the signal that the pattern is wrong rather than
the prose, and the measurement to re-run is the hit-by-hit listing over
`_model_facing_descriptions()`, which the guards' own assertion messages print. The file that would
show it is `tests/test_prose_contract.py::_TRUE_ABOUT_WHAT_IS_GONE`.
