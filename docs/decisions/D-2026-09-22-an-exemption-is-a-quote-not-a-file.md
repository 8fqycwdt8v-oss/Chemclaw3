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

**Measured.** Widening the universe to every `SKILL.md` a turn can load — the bundle-local ones and
the repository's own layer-3 skills, both of which the model reads whole — the three patterns
produce **eight hits and every one of them is correct text**. A 100% false-positive rate, which is
what the row predicted and why it is a row rather than a widening.

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

- **Two narrowings that are correct by construction, not concessions.** `conceptual[- ]DFT` leaves
  the tier pattern because it names a *shipped* panel — `publish/properties.py`'s `conceptual_dft`
  group, projected by `publish/project.py` from GFN2-xTB frontier orbitals, reaching no DFT tier —
  so three skills would otherwise need an exemption apiece for saying something true about a
  capability that ships. And `propose[sd]? (?:what|it|them|the)` is dropped: over the widened
  universe it catches **zero** of the three shipped `synthesize_memory` defects the gate guard
  exists for (all three are caught by `for review` and `pull request`) and **four** correct
  sentences, every one of them proposing an *experiment* rather than a review — which is the
  behaviour `D-2026-08-26` asks for in as many words. What is left requires the review *object*,
  which is the thing that was deleted.

- **The recall widening the row named is free.** `PRs`, `review queue`, `knowledge gate`, `awaits
  review` cost **zero** new hits over the widened universe, so the phrasings a future writer would
  reach for are closed at no cost to the false-positive rate.

- **Three exemptions remain, and each is a literal quote that contains its match.** Not a file name
  and not a pattern: a new DFT sentence added to an exempted skill still reds, because the
  exemption covers that span and nothing else. This is the shape `_A_LIVE_GATE` already took for
  `propose_skill` — per tool and partial, never per pattern — one level finer.

## Consequences

**Two guards keep the exemptions honest, because an allowlist is the easiest control to leave
behind.** `test_every_exemption_still_quotes_shipped_prose` fails the day one of these sentences is
reworded, so an exemption cannot outlive its text and lie in wait to cover some *other* sentence
later; `test_every_exemption_is_needed` fails if a pattern is narrowed past an entry, so an
exemption cannot become decoration. That pair is the same defect `_A_LIVE_GATE`'s citations turned
out to be — read by nothing until
`D-2026-09-18-a-control-that-names-a-module-is-a-claim-about-where-somebody-put-the-code` — caught
one layer earlier this time.

**The universe is driven, not asserted.** `test_a_forbidden_sentence_in_any_of_them_is_caught`
poisons each loader in turn with each forbidden sentence and requires the guard to red *on the
poison alone*, with a control call first, so a class that silently stops being scanned fails here
rather than going quiet. That is the row's own reproducer kept as a test: it drove every forbidden
string at once into `connectors/results/connector.yaml`'s job description and both guards stayed
green.

**No count is written in the docstring any more.** The one that was there said 31 registered tools
against a surface of 114 and both had moved.

**Revisit when:** a shipped sentence needs a fourth exemption, or an exemption needs a second quote
for the same sentence. Three entries is small enough to read; a list that grows is the signal that
the pattern is wrong rather than the prose, and the measurement to re-run is the one above — the
hit-by-hit listing over `_model_facing_descriptions()`, which the guards' own assertion messages
print. The file that would show it is `tests/test_prose_contract.py::_TRUE_ABOUT_WHAT_IS_GONE`.
