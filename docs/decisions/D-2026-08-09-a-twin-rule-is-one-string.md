# D-2026-08-09-a-twin-rule-is-one-string — A twin rule is one string, and a guard must be measured

**Status:** accepted · **Date:** 2026-08-09

Supersedes the hydrazine paragraphs of
[`D-2026-08-08-a-partial-answer-must-say-so`](D-2026-08-08-a-partial-answer-must-say-so.md) §3, which
this decision found to be half-applied and cited to a set that cannot support it. That ADR is left
untouched, as a merged ADR must be; this one is the record of what changed and why.

## Context

`science/safety/rules.yaml` carries a hazard motif twice: once as a `structural` alert, and once as
an arm of an `incompatible_pairs` rule. The pair rule is the one that fires on a *reaction*, so it
is the one that decides whether a chemist is warned before mixing two things.

That duplication has now produced the same defect twice. The `peroxide` widening reached the
structural rule and not its pair arm, and the fix for it left a comment saying so: *"A rule and its
structural twin must be widened together — half a fix reads exactly like a clean screen."* The
hydrazine widening in D-2026-08-08 then did the same thing, in the edit that carried that very
sentence. The structural rule dropped the requirement of an N–H on *both* nitrogens, by name, for
1,1-dimethylhydrazine (UDMH); the pair arm gained `NX4+` and kept the H requirement.

Measured at `8901e9d`, `screen_reaction([x, "OO"])`:

| molecule | structural `hydrazine` | `oxidizer-with-reductant` |
|---|---|---|
| hydrazine `NN` | fires | **fires** |
| hydrazine·HCl `[NH3+]N.[Cl-]` | fires | **fires** |
| UDMH `CN(C)N` | fires | **silent** |

UDMH with H₂O₂ or N₂O₄ is the archetypal hypergolic pair — it ignites on contact, with no ignition
source. It was the single case in the table that screened clean on the rule named for it, and it
was the exact molecule the rule set had already learned about once.

A second, independent defect sat forty lines above. The structural rule carried `!$(N[a])`,
documented as what *"keeps aryl-diazo/azo systems out"*. Measured, that is not what it did:

| molecule | with `!$(N[a])` | without |
|---|---|---|
| azobenzene | False | **False** |
| phenylhydrazine | True | True |
| 1,2-diphenylhydrazine (hydrazobenzene) | **False** | True |
| 1-methyl-1-phenylhydrazine | **False** | True |

An azo nitrogen is `NX2`; `[NX3,NX4+]` can never match it, with or without the guard. The guard's
only measured effect was to silence hydrazines — and only when *both* nitrogens were aryl-bound,
since with one the match is found from the other direction, which is why it looked harmless.
Hydrazobenzene is a routine nitrobenzene-reduction product and a benzidine-rearrangement precursor.

## Decision

**1. A motif that appears as both a structural rule and a pair arm is spelled with one string.**
After this change the `oxidizer-with-reductant` right arm embeds the `hydrazine` rule's SMARTS
verbatim, and `tests/test_safety.py::test_the_hydrazine_pair_arm_is_its_structural_twin_verbatim`
asserts the two are character-identical. Pinning molecules cannot prevent the third occurrence of
this defect, because the next divergence will be a molecule nobody listed; pinning the strings
equal can. `left` keeps its own spelling deliberately — an oxidiser arm is a union over four
structural rules, not the twin of any one of them.

**2. `!$(N[a])` is removed, and the real reason azo systems are excluded is written down.**
Coordination, not an aryl guard. A guard whose stated purpose is served by something else, and
whose measured effect is the opposite of its comment, is worse than no guard: it reads as
deliberate scope.

**3. A widening is measured in both directions, over a panel that could actually show a change.**
Measured over 106 structures — the 61 distinct structures of the reagent identity table plus 45
hand-picked hydrazine-adjacent and nitrogen-bearing bench reagents — exactly five molecules change
verdict across both rules, and every one is a hydrazine: hydrazobenzene and
1-methyl-1-phenylhydrazine gain the structural rule; UDMH, N-aminopiperidine, N-aminomorpholine and
1-methyl-1-phenylhydrazine gain the pair arm. Nothing moves in the false-positive direction — no
amine, ammonium, hydroxylamine, urea, azo, hydrazone or acyl hydrazide — and tetramethylhydrazine
stays out for want of an N–H. `_NOT_A_HYDRAZINE` in `tests/test_safety.py` carries that panel.

The prose and citation already covered every newly matched molecule ("free hydrazine motif …
toxic, a suspected carcinogen, and a strong reductant that forms energetic mixtures with
oxidizers"; Bretherick's, *hydrazines*), so the patterns did not outrun their justification and no
explanation moved.

## The measurement that was cited, and does not reproduce

D-2026-08-08 §3, `rules.yaml` and `tests/test_safety.py` each said: *"Measured across the 83
distinct structures of the reagent identity table, the widening newly matches … hydrazinium salts
and **nothing else**."*

Re-measured at `8901e9d`: `chemclaw.core.reagents._TABLE` holds **87 names over 61 distinct
structures**, not 83. Running the old and new patterns over all 61, **zero** structures change
verdict — for all three widenings — because the table contains no hydrazine in any form. Its only
N–N structures are two azides and the triazole of TBTU/HATU.

So the set could never have established the claim. It establishes only "matches nothing new
*here*", which is a much weaker sentence and is not the one that was written. The number was wrong
and the inference was unsupported, and it had already been copied to three places. Corrected in
`rules.yaml` and in the test; recorded here rather than edited into the merged ADR.

This is the standing rule restated: a claim is measured over a set that could have falsified it, or
it is not measured.

## Consequences

- UDMH, N-aminopiperidine and N-aminomorpholine beside an oxidiser now raise
  `oxidizer-with-reductant`; hydrazobenzene and 1-methyl-1-phenylhydrazine now raise `hydrazine`.
  All five are advisory flags, as every rule in this table is.
- One more sentence a future narrowing has to answer: the twin-equality test names no molecule, so
  it fails on any divergence rather than on a listed one.
- The reagent identity table remains hydrazine-free. Extending it is the obvious way to make the
  identity table a set that *could* check a hydrazine widening; it is filed in `BACKLOG.md` rather
  than done here, because adding reagents to a shared table is a change with its own review.
