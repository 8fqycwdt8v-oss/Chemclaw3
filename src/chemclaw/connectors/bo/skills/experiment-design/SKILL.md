---
name: experiment-design
description: >-
  Use when the chemist has supplied, or the record holds, two or more runs that vary the same
  numeric or categorical factors and report a numeric outcome (yield, purity, conversion, ee) —
  a screen, an optimization series, a factor table. Turns that into a concrete
  Bayesian-optimization problem, calls suggest_next_experiment or generate_screening_design, and
  presents the proposal as something a human still runs. Also for explaining why the optimizer
  chose a point (explore vs exploit) and for judging whether a campaign has plateaued. Applies
  however the ask is phrased, including "what should I run next/tomorrow" — a clean table of runs
  is the trigger, not the wording.
tools:
  - suggest_next_experiment
  - resume_campaign
  - generate_screening_design
  - campaign_progress
  - propose_knowledge_note
---

# Experiment design

Holds the *judgment* for the next-experiment question; the mechanics are in
`suggest_next_experiment` (BoFire's ask step). A good suggestion is only as good as the problem
you hand it, so most of the work is framing, not the call.

**First decide the question is this one.** A surrogate needs a scalar objective over bounded
variables and enough runs to fit. When the chemist is instead walking one step day by day, and
what they want is a reading of where the series got to and one diagnostic to run next, that is
the `experiment-progression` skill — reasoned from the record, not from a model. Do not force a
line of enquiry into a design space just because a design space is what this tool takes.

## Frame the problem from evidence

1. **Fix the objectives.** Each is a name and a direction — maximize yield, minimize an impurity,
   minimize E-factor. **If the user names several, give them all.** `objectives` is a list, and the
   optimizer searches the trade-off rather than one axis. This is the one instruction in this file
   that reversed: it used to say pick the one they lead with, because nothing could do more.

   With more than one objective, every run you supply must report **every** objective, in its
   `values` map — the tool refuses an observation that reports fewer, naming which. What comes back
   is a `front`: the runs among those you supplied that nothing else beats on every objective at
   once. **Present that front and let the chemist choose along it.** Do not announce a single best
   point for a trade-off; there is not one, and saying otherwise is the same overclaim in the
   opposite direction from the old refusal. Where the front has one member, say that too — it means
   one run dominated every other, which is a real and unusual finding.

   Keep a single objective when the chemist has one. A trade-off between yield and a cost the
   record does not measure is not a second objective; it is a conversation.

   **A limit across several parameters is a constraint.** "Base plus acid under 3 equivalents",
   "water at most 5% of the solvent", "the three fractions sum to 1" go in `problem.constraints`,
   and the optimizer honours them — every candidate it returns satisfies them, seed points included.
   Three things to keep straight, because getting them wrong is silent:

   - A limit on **one** parameter is that parameter's **bound**, not a constraint. Writing "T under
     80 °C" as a constraint instead of an upper bound is a worse way to say the same thing.
   - The linear form is **continuous only**. A forbidden *pairing* of categorical options — "never
     Pd(OAc)₂ in DMSO" — is the other constraint shape, an exclusion, and it needs an
     all-categorical problem; a forbidden option on its own is one you leave out of the list.
   - Write the relation the chemist stated. `>=` is supported directly, so do not flip a limit round
     by hand — an inverted constraint is the one mistake here that yields a confident wrong answer
     instead of an error.

   `generate_screening_design` **refuses** a problem carrying constraints: a factorial screen
   enumerates corners and honours no limit, so it would hand back runs that violate one.
2. **Choose the decision variables** the user can actually change: continuous (temperature,
   time, equivalents, concentration) with realistic bounds, categorical (solvent, catalyst,
   base) with the specific options in play. Do not invent variables the lab cannot set, and
   keep bounds physically sane — a bound outside the range a real run of this reaction class
   could survive (e.g. well past a solvent's reflux/decomposition point) is not something to
   quietly accept and flag only after the tool returns a point there. Confirm the bound with
   the chemist *before* calling the tool. The same applies to genuinely ambiguous input (e.g.
   a self-correcting or contradictory statement of the range) — ask which value they meant
   rather than silently picking an interpretation and proceeding.

   **When a categorical option is a molecule, give its structure.** Set the parameter's
   `structures` (category label → SMILES) for ligands, bases, solvents and catalysts. Each
   option is then described by computed electronic descriptors instead of being an opaque
   label, which is what lets the model say anything at all about an option nobody has run:
   without it the surrogate's prediction for an untried ligand is just the average of the
   ones you did run. It costs one fast calculation per option, cached thereafter. Two limits
   worth stating to the user: the descriptors are **electronic only** — two ligands differing
   mainly in bulk look similar — and a wrong SMILES silently describes the wrong molecule, so
   only supply structures you are sure of.
3. **Seed with real runs.** Gather the transformation's history (`find_similar_reactions`, an
   `optimization-campaign` note) and turn each run into an observation: its conditions →
   objective value. Mark `provenance` "measured" for lab data, "predicted" if you filled a
   value from a model. With no runs on file, the tool returns space-filling seed points — say
   the campaign is starting cold.

## Narrowing a categorical space before you frame it

When the user brings more candidate options than the campaign can carry — twelve ligands, eight
bases, a substrate scope — the choice of *which* to put in the design is itself a decision, and
it is made before the tool is called. A fast electronic ranking
(`compute_electronic_properties`, `predict_site_reactivity`; judgment in
`reactivity-descriptors`) is a legitimate way to shortlist, because ranking is what a
semiempirical method is actually good at and the cost of being wrong is one wasted run.

Two conditions on doing this. Say that the shortlist came from a calculation, not from data, so
the user can overrule it. And keep at least one option in the design that the ranking did *not*
favour — a campaign seeded only with what a model already liked cannot discover that the model
was wrong.

## Call and present

- `suggest_next_experiment(problem, observations, count)` returns the candidate point(s), the
  `campaign_id` they belong to, and the `calc_refs` behind the decision space. Ask for a small
  batch (1–3) unless the user wants a screen.
- **These are proposals, not results.** Present each as conditions to run, note it rests on the
  cited historic runs, and be explicit about what the model is extrapolating (a solvent never
  tried, a temperature beyond the observed range) and any safety/selectivity risk there.
- **Quote the `campaign_id` back.** The campaign is identified by its decision space, so asking
  again about the same problem — with the run you just did added to `observations` — accumulates
  onto the same campaign instead of starting over. That id is how a chemist, or you in a later
  session, picks the thread back up.
- If the user wants the batch recorded, draft it through `propose_knowledge_note` with type
  `experiment-proposal` so a human approves it via the PR-gate before it becomes plan-of-record.
  Pass the returned `calc_refs` on that note: the descriptors that shaped the space came from real
  calculations, and citing them is what lets a stale one be traced to the experiments it
  suggested.
  That is the same type a reasoned proposal uses — the note body says which path produced it, and
  a reviewer approving "run this next" should not have to learn two note kinds for one decision.
  `bo-candidate` is a different thing and is not yours to write: it is what a *durable* campaign
  (`start_optimization_campaign`) mints for itself when a round completes.

## Reading the sd: is this point an exploit or an excursion?

`suggest_next_experiment` returns each candidate's `predicted_sd` — the surrogate's posterior
spread at that point, before anyone runs it — and a `scale` giving what the objective actually
spans in the runs you supplied. **The sd only means something against that spread**, and the
return's `summary` already makes the comparison; quote it rather than recomputing it.

- A small sd relative to the spread is an **exploit**: the model is refining chemistry it has
  already learned, and the predicted value is the part worth quoting.
- A large one is an **excursion**: the model is proposing a region it has no evidence about, so the
  predicted value is close to meaningless and the reason to run it is information, not yield. Say
  that plainly, and name what is being extrapolated — a solvent never tried, a temperature beyond
  the observed range — along with any safety or selectivity risk there.
- `predicted_sd` of `None` is **not** a small sd. It means the point came from the space-filling
  seed design and no surrogate had an opinion at all. Never present a seed point as a model
  recommendation.

Do not turn the sd into a confidence interval on the outcome. It is what the model believed
*before* the experiment; the measured value will come from the assay, and the two are different
quantities.

## Has it plateaued?

When the chemist asks whether to keep going — "have we plateaued", "is there more in it", "I don't
want to burn another two weeks" — **answer that question rather than reflexively proposing another
candidate.** `campaign_progress(...)` takes the decision space, the runs in the order they were
performed, the assay's own noise and a window, and reads the runs back: the best so far, how many
evaluations since a gain larger than that noise, and whether the most recent results differ from
each other at all.

**Get `assay_noise` from the chemist.** It is the assay's reproducibility in the objective's own
units — "reproducibility is about ±2%" is `assay_noise=2.0` for a yield. If they have not said,
**ask before calling**. Do not supply one yourself: a 1–2% gain against a ±2% assay is not a gain,
and asserting that it is has already been graded a fabrication once.

Two calls, two different claims, and they must not be merged:

- `campaign_progress` says what the **record** did. It asks no surrogate.
- `suggest_next_experiment` says what the **model expects**. Compare its candidates'
  `predicted_value` against the same `assay_noise`: a predicted improvement inside the noise is not
  a reason to run another experiment, and a large `predicted_sd` somewhere untried is — that is the
  difference between "nothing left here" and "nothing left *where we have looked*".

Close with the limit the return states and never round it up: this shows that recent points in the
region already explored have not beaten the noise. It cannot show a global optimum has been
reached, and an untried corner of the space is not evidence either way. Then give the leader a
decision they can defend — a stopping criterion, or one specific run and what it would have to
return to change the conclusion.

## Picking a campaign back up

When the chemist returns with a result — "the 85 °C run in toluene gave 71%" — or asks where an
optimization got to, **call `resume_campaign(campaign_id)` before framing anything**. It returns
the decision space as it was last framed, the observations the previous suggestion rested on, and
the candidates it proposed. Append the new result to those observations and call
`suggest_next_experiment` with the *returned* `problem`, so the campaign continues rather than
restarting on whatever fragments survived in the conversation. Re-typing the space from memory is
how a campaign silently forks: the id is a hash of the space, so one bound written differently is
a different campaign with no history.

If the id does not resolve, the tool raises. That is nearly always the space having changed since —
a widened range, a ligand added — which makes it a *new* campaign, not a lost one. Say so plainly,
and ask for a fresh suggestion over the current space; do not guess at an id.

## One shot vs. a campaign

`suggest_next_experiment` is the single, human-in-the-loop suggestion: it answers "what should I
run next?" inline, from observations you already have.

A fully automated loop that proposes, evaluates its own objective, and iterates over many rounds is
`start_optimization_campaign` — reach for that only when the objective can be computed without a
human in each round. It is durable and long-running, so it returns a job id immediately; poll it
with `get_durable_job_status`. Its recommendation is always proposed as a PR-gated note, so a human
reviews it; you do not decide whether the campaign is recorded.

State the campaign's `rationale` in the chemist's terms — the question this campaign should answer
and what prompted it — not a restatement of the parameter ranges. It is stored with the run and
printed on the note a reviewer signs, and it is what a session six months from now will read when
it asks `find_past_jobs` whether this optimization has already been done. Before starting a
campaign, run that search: a near-identical campaign that already ran is evidence to build on
(seed the new one with its observations), and only an *identical* one rejoins its result for free.

## The other DoE question: a categorical screen, not an adaptive suggestion

`suggest_next_experiment` and `start_optimization_campaign` both propose points *adaptively*, one
batch at a time. Sometimes the real ask is the classical, complete-up-front design instead —
"give me every combination of these catalyst/solvent/base choices to screen" before narrowing to
BO. That is `generate_screening_design(problem)`: a factorial design over the problem's factors.
Present the returned runs as a batch a human executes, exactly like a BO suggestion.

**Continuous factors belong in a screen, and you no longer discretize them by hand.** Declare
temperature or equivalents as the continuous parameters they are; the screen holds each at the two
ends of its declared range. Two things to say to the chemist when it does: the design tests
*whether* that factor matters, not *where* in the range the optimum is; and a column reading 20/120
is a collapsed range rather than a considered pair of levels — the return names every factor
treated that way, so use its wording. If the real question is where in the range to sit, that is
`suggest_next_experiment`, not a screen.

**Size the grid against the chemist's budget before you present it.** Multiply the level counts:
seven two-level factors is 128 runs, which does not fit a 96-well plate, and handing over a design
the chemist cannot execute is not an answer. When it does not fit, pass `n_generators` — each
generator halves the design (128 → 64 → 32 → 16). Two rules on doing that:

- **Only when the budget requires it.** A full grid that fits is always the better design; a
  fraction buys runs by confounding effects.
- **Never present it as complete.** The return carries a `resolution` and a one-line `summary`
  saying which effects are now confounded — resolution III means an effect you attribute to one
  factor may belong to a pair of the others, IV means the main effects are clean but two-factor
  interactions are not. Repeat that sentence to the chemist alongside the run list. A reduced
  design shown as "here is your screen" is the failure mode; it looks exactly like a smaller
  complete one.

A reduced design needs every factor to have exactly two levels. A three-level categorical is
refused, not quietly crossed in full — pick the two levels that matter, or run the full grid.

**Three knobs that make a screen worth analysing, and when to reach for each.**

- **Centre points** (`n_center`) sit at the midpoint of every continuous factor, and they are the
  only thing in a two-level screen that can see curvature. A factor that helps up to a point and
  then hurts reads as "no effect at all" without them, which is the most expensive way a screen can
  mislead. Ask for them whenever a continuous factor is in play and the chemist cares whether the
  effect is monotonic. Watch the count when you present it: they are added **per combination of the
  categorical factors**, so the total is not the corner count plus the number you asked for — read
  the run count off the returned design rather than computing it.
- **Replication** (`n_repetitions`) is what gives the screen a pure-error estimate. Without it, no
  effect the screen reports has a significance you can honestly quote — you can say a factor moved
  the number, not that it moved it more than the assay would have anyway.
- **Randomized run order** (`randomize`) protects against a drift over the session — a decaying
  reagent, a warming room — being read as a factor effect. It costs nothing. Tell the chemist to
  run them in the order returned, or the protection is lost.

Both `n_center` and `n_repetitions` need at least one continuous factor and are refused on an
all-categorical problem, because BoFire ignores them there. If the chemist wants replicates of an
all-categorical screen, say that they should run the returned list twice.
