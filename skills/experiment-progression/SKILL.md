---
name: experiment-progression
description: >-
  Use when the series is qualitative or mechanistic rather than a factor table — runs that
  changed several things at once, failed for different reasons, or need a diagnostic before the
  next real experiment. Read the series in time order, name what each run tested and showed, and
  propose one experiment with its rationale and a falsifiable expectation, from the record and the
  calculators rather than a surrogate model. When the runs instead vary the same factors and
  report a numeric outcome, that is a fitted decision space: use experiment-design and call the
  optimizer rather than reasoning the next point by hand.
tools:
  - gather_evidence
  - expand_note
  - similar_reactions
  - predict_solubility
  - predict_pka
  - compute_xtb_energy
  - predict_site_reactivity
  - screen_hazards
  - propose_knowledge_note
---

# Experiment progression

The setting this exists for: one technician, one step, one experiment a day for weeks, each run
chosen in response to yesterday's result. The question is not "optimize this" — it is "here is
where I got to; what is the next thing worth doing, and why that one".

This is the **agent-reasoned** path. Its sibling is `experiment-design`, which frames a decision
space and asks BoFire for the next point. Choose between them explicitly:

- **This skill** when the chemist wants the reasoning — the series is short, the variables are
  qualitative or mechanistic, the interesting question is *what the last few runs mean*, or the
  next move is a diagnostic ("is the impurity from the base or the water?") rather than a step
  in a numeric space.
- **`experiment-design`** when the objective is one scalar over a few well-bounded continuous or
  categorical variables and enough runs exist to fit a surrogate — a real optimization.

Say which one you used and why. Never present a reasoned proposal as if a model produced it, or
a model's point as if it were reasoned from the record.

## 1. Reconstruct the series before saying anything about it

- Start from the `optimization-campaign` note for the transformation: it lays the runs out **in
  the order they were performed**, with each run's date, conditions, outcome, what it changed
  relative to the run before, and — where the ELN captured it — the hypothesis it was testing.
  `gather_evidence(note_type="optimization-campaign")`, then `expand_note` for the member runs.
- **Check the ordering caveat at the top of that note.** If it says no run carries a date, you do
  not have a timeline: you have a set. Analyse it as a set (which is still useful) and say so,
  rather than narrating a progression the record cannot support.
- Windowed questions ("what have I tried since the review", "the last two weeks") use
  `gather_evidence(since=…, until=…)`. Undated notes are excluded from a windowed sweep, so if
  the answer looks thin, re-run without the window before concluding nothing was done.
- Read the **failures** as first-class data. A run recorded with `outcome: failure` and its
  reason has usually eliminated more of the space than a mediocre success did.

## 2. Say what the series has established, and be strict about it

Three separate statements, never blended into one:

- **Established** — supported by a controlled comparison in the record. Two runs differ in one
  condition and the output tracks it. Cite both runs. Anything less is not established.
- **Confounded** — several conditions moved at once and the outcome changed. Say exactly which
  runs and which conditions; do not credit one of them.
- **Untouched** — a variable nobody has moved across the whole series. This is often the most
  valuable thing you can report, and it is invisible unless you look for it: scan the "changed vs
  previous" column across every row and name what never appears in it.

The discipline of `optimization-campaign-synthesis` applies in full here — it is the same data
and the same trap. Correlation across a messy screen is not a lever.

## 3. Read the intent, do not invent it

- The `tested:` line on a run is the chemist's own statement of what it was for. Use it: it tells
  you whether a condition change was the point of the run or incidental to it.
- Where it is absent, the honest reading is "conditions changed from X to Y; the record does not
  say why". **Do not reconstruct a motive from the change.** A date proves one run came after
  another; it never proves it was run *because* of it — this is exactly why the system does not
  mint `follows` edges from dates on its own.
- If knowing the intent would change your proposal, ask. One question to the technician
  ("was the switch to 2-MeTHF about the impurity or about the workup?") is worth more than a
  paragraph of inference.

## 4. Compute what the record does not know

Before proposing anything that rests on an untested property, calculate it and fold the number
in with its uncertainty — solubility in a solvent nobody has tried, a pKa that decides whether
a base is strong enough, a relative stability, a site's reactivity. A proposal that says "try
2-MeTHF" is a guess; one that says "try 2-MeTHF — the substrate's predicted solubility there is
comparable to THF (x mg/mL), which was the reason THF was chosen" is an argument. See
`computational-evidence` for how far to trust each calculator.

## 5. Propose exactly one experiment (with a fallback, not a list)

The technician runs one experiment tomorrow. A list of five is a way of avoiding the question.

State, in this order:

1. **The proposal** — the concrete conditions, complete enough to run: what changes from the
   last run, and what deliberately stays fixed.
2. **The rationale** — which runs (cited) and which computed values it rests on, and which of
   the three statements in §2 it follows from.
3. **The expectation, falsifiable** — what you expect to see, and *what result would refute the
   reasoning*. "If the impurity is still there at 60 °C, it is not thermal and the base is the
   next suspect." A proposal that no outcome could contradict has told the chemist nothing.
4. **The fallback** — the one thing to do instead if the expectation fails. This is what makes
   the proposal part of a line of enquiry rather than a shot.
5. **What it will not tell you** — the confound you are accepting, if any.

Keep the proposal inside what the lab can actually do. If the obvious next experiment needs
equipment or material that has never appeared in the series, say so instead of quietly assuming
it exists.

## 6. Record it through the gate

Write the proposal as an `experiment-proposal` note via `propose_knowledge_note`, so tomorrow's
session knows what was suggested and can compare it against what actually happened:

- Link the run it responds to with `[[follows:reaction-<id>]]` — that edge is the line of enquiry,
  and this is the one place it is legitimately minted, because here the intent is known: it is
  yours.
- Cite the evidence runs and any campaign note with plain `[[wikilinks]]`, and name the computed
  values you used.
- It is a **proposal**: a human decides whether to run it (D-005). Write it so the reviewer can
  see the reasoning and reject the premise, not just the conditions.

Safety is not optional in a proposal. If the suggestion moves into a regime the series has not
been in — a higher temperature, a new oxidant, a change of scale — run `screen_hazards` on it
and put the result in the note, or state plainly that a safety review is needed first.
