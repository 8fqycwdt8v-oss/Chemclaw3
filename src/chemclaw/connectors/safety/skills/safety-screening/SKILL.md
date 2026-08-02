---
name: safety-screening
description: >-
  Judgment for the three safety tables: when to call screen_hazards, screen_genotoxic_alerts and
  ich_impurity_limit, which question each one answers, how to report a flag so a chemist can act
  on it, why an empty result is never "safe", and where each table's competence ends and a human
  assessment begins. Load this before answering anything about mutagenicity, genotoxicity, ICH M7,
  nitrosamines, elemental impurities or residual solvents — three of those now have a table and
  none of them has a model, and the difference is the whole content of this skill.
---

# Safety screening

Holds the *judgment* for the `safety` bundle's three tools. Each is deterministic and has no
opinion; this skill decides when to call which, and — far more importantly — how to talk about
what comes back.

## Three questions, three tables. Do not mix them.

| The chemist is asking | Tool | What it reads |
| --- | --- | --- |
| Is this safe to run today? | `screen_hazards` | A curated SMARTS table of energetic and reactive motifs, plus dangerous combinations between a reaction's components |
| Will this need a mutagenic-impurity control strategy? | `screen_genotoxic_alerts` | A cited table of DNA-reactive structural alerts, plus the nitrosamine formation route |
| What is the limit? | `ich_impurity_limit` | Transcribed ICH Q3C residual-solvent classes and limits, and ICH Q3D elemental-impurity PDEs |

These are different questions with different controls and different readers. Answering one with
another is the single most damaging mistake available here: reporting a process-safety screen as a
regulatory-toxicology verdict is how a table of energetic motifs becomes an ICH M7 assessment.
When a question spans two of them, call both and report them as two separate findings.

## The one rule that matters

**The screen flags; it never clears.** No flag means *no rule in the table matched* — it says
nothing about toxicity, exposure, thermal stability of the specific compound, scale, impurities,
or the process around the reaction. Never write "the reaction is safe", "no hazards", "safe to
run", or any phrasing a reader could take as clearance. Say what is true: *"No rule in the hazard
table matched. This is not a safety assessment."*

An over-trusted screen is more dangerous than no screen, because it turns an absence of knowledge
into apparent assurance — and a chemist who has been told "no hazards" three times stops reading
the fourth answer.

## When to call it

- **Always, before proposing chemistry**: any synthesis route, reagent choice, condition set, or
  procedure you are about to recommend or write into a note. Screen every species you name —
  reactants, reagents, solvents, products, and any intermediate you propose isolating.
- **Pass the whole reaction, not one molecule at a time**, when several species meet. Component-wise
  screening cannot see an oxidizer/reductant pairing: each component is unremarkable alone.
- **On retrieved precedent too**, when you are about to recommend repeating it. A procedure being
  in the ELN means someone ran it once, not that it is safe to scale or to repeat unchanged.
- Not needed when you are only reporting a historical result and recommending nothing.

## How to report a flag

1. **Lead with it.** A high-severity flag goes at the top of the answer, not in a closing caveat.
2. **Give the explanation and the citation**, not just the rule id. "Contains an organic azide
   (energetic, potentially shock-sensitive; Bräse 2005)" is actionable; "flag: organic-azide" is
   not.
3. **Name what the chemist should do next** in their terms — a process-safety review before
   scale-up, in-situ generation instead of isolation, an SDS check, an EHS conversation. The
   rule's `explanation` field carries the standard control; use it.
4. **Do not soften or dismiss a flag** because the compound is common or the literature routinely
   uses it. Report it and let the human weigh it. Equally, do not editorialize a flag into a
   refusal: the chemist decides whether the work proceeds, with their controls.
5. **A flag is not a reason to withhold the chemistry.** Answer the question *and* flag the
   hazard; an answer that omits the route because it is energetic helps no one.

## Writing it into a note

Any agent-proposed note describing a procedure must carry a `## Hazards` section when the screen
returns a flag at or above the configured gate severity — `kg-validate` enforces this, so a note
missing it fails the PR check rather than reaching a reviewer without its warnings. Write the
section as the flags plus their standard controls, and state plainly that it is a structural
screen, not an assessment.

## Where this ends

The screen covers *structural motifs with a documented hazard*. It does not cover, and you must
not imply it does: toxicity or carcinogenicity, occupational exposure limits, regulatory or
transport classification, thermal-stability data (DSC/ARC), incompatibilities beyond the pairs in
the table, quantities and scale, or engineering controls. Those need an SDS, the site's EHS
function, and — for anything energetic — a process-safety review. When a question turns on one of
them, say so and point there rather than guessing.

**Four classes the hazard table does not address at all**, which matter because a chemist may
reasonably expect a "hazard screen" to: mutagenicity and genotoxicity, nitrosamine risk, elemental
impurities, and residual solvents. Running `screen_hazards` and reporting its result as any of
these converts sixteen process-safety motifs into a regulatory toxicology verdict. Three of the
four now have a table of their own — and each comes with a limit on what it can say.

## What the two newer tables can and cannot say

**`screen_genotoxic_alerts` gives you alerts. It does not give you a classification.** An alert is
a DNA-reactive structural motif with a published citation. An ICH M7 class, an acceptable intake,
a purge factor and a mutagenicity prediction are outputs of a model — two complementary (Q)SARs
plus an Ames corpus and expert review — and this system has none of them. So:

- Report a flag as *"this motif is a structural alert; it requires expert assessment"*, giving the
  motif and its citation. Never as a class, a limit, or a probability.
- Never produce a worked purge-factor calculation, even as an illustration. In the live run that
  preceded this table, an invented purge factor and invented acceptable-intake limits were the
  exact failure — and they were invented *because* the model felt obliged to finish the answer.
  Finishing the answer is not your job here; naming what an expert has to do is.
- An empty result is not a negative prediction. The table is nine alerts long; "nothing matched"
  means nothing in it matched.
- For nitrosamine risk, pass the **whole route**, not one step. The formation alert fires when a
  nitrosatable amine and a nitrosating agent appear in the same call, so a nitrosating agent
  introduced two steps later is invisible to a per-step call. Say so when it applies.

**`ich_impurity_limit` gives you a transcribed number with its citation. Never recall one.** Quote
the limit *with* the guideline, revision and table it came from, so the chemist can check it. On a
miss, say the tables here do not carry that substance and point at the guideline — do not
substitute a similar substance, and do not fall back on memory. A recalled limit that happens to
be correct is worse than a wrong one: it trains the reader to trust the next. And a limit is not a
risk assessment — whether a process needs a control, and what specification an intermediate should
carry, are judgements the number feeds rather than settles.

**Computation does not extend the screen either.** A semiempirical calculation can estimate a
decomposition energy or the stability of an energetic motif, and a user will eventually ask for
one. It may be used to *triage* — to decide which compounds get sent for calorimetry first — and
it may never appear in an answer as reassurance. A computed number is not a DSC, an ARC, or a
process-safety assessment, and "the calculation says it is fine" is exactly the false clearance
the rule above forbids. Report the calculation as what it is, keep the screen's language
unchanged, and still point at the process-safety review.
