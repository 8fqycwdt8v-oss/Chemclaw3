---
name: ionization-and-partitioning
description: >-
  Judgment for using a predicted pKa in process decisions — extraction and wash pH, salt
  and counterion questions, ionization state — including what makes it dangerous:
  individual values miss by up to two units, and aliphatic amines are refused outright
  while aromatic nitrogen is covered.
tools:
  - predict_pka
  - predict_solubility
---

# Ionization and partitioning

Holds the *judgment* around `predict_pka`. The mechanics are in the tool; this skill
decides what a predicted pKa may and may not be used for, and it exists mainly to stop
one specific failure: quoting a computed pKa into a pH decision that a two-unit error
would invert.

## The two rules, before anything else

**1. Rank with it. Never set a pH with it.** Benchmarked against 12 experimental values
(the calibration and its per-class Spearman figures now live with the engine, in
`Chemclaw3-mcp:servers/calc/src/chemclaw_mcp_calc/engine/pka.py`): the *ordering* is reliable
(Spearman ρ 0.965), individual values
miss by up to **2.1 units**, and the reported uncertainty is ±1.6. Process rules of thumb
— "extract two units below the pKa", "ΔpKa > 2–3 for a stable salt" — turn on exactly
that margin, so a plausible-looking prediction can invert the decision. If a pH, a salt
form, or a specification depends on the number, the answer is **"measure it"**, not a
computed value with a caveat attached.

**2. Basic nitrogen splits in two, and only one half is covered.** The tool returns an
acid pKa for a neutral O-H/S-H site (carboxylic acids, phenols, alcohols, thiols) and,
when there is no such site, the **conjugate-acid pKa (pKaH)** of a basic nitrogen — but
only an aromatic or aryl one.

- **Aromatic / aryl nitrogen is covered**: pyridines, imidazoles, triazoles and other
  azoles, anilines. Fitted over seven references spanning pKa 1.0–6.95 at Spearman
  **1.000** (R² 0.993, worst error 0.37 units), and reported with ±1.0. This is the
  *better* of the two calibrations, not a weaker fallback.
- **Aliphatic amines are refused** — the tool raises rather than returning a value. Over
  13 reference amines the computed basicity ranks them at Spearman **−0.17**: no ranking
  ability at all. Do not work around this. The cause is diagnosed and no recalibration
  fixes it: in the gas phase GFN2 gets the proton affinities exactly right
  (NH₃ < MeNH₂ < Me₂NH < Me₃N), switching on the continuum solvent *reverses* that order,
  and the true aqueous order is neither — it is non-monotonic
  (Me₃N < NH₃ < MeNH₂ < Me₂NH), because aqueous aliphatic amine basicity is set by how
  many hydrogen bonds the ammonium ion donates to water. A continuum has no water
  molecules to donate to, and a linear map cannot recover a non-monotonic relationship.

So when an API is a plain aliphatic amine — a great many are — say plainly that the value
is not predictable here and that it must be measured or taken from literature. Do not
reach for a different tool and present its output as a pKa.

Say which of the three cases you are in every time: acid site, aryl-nitrogen pKaH, or
refused. They are different numbers about different equilibria.

## What it is genuinely good for

- **Ranking a congeneric series.** Which of six phenol analogues is most acidic; how a
  substituent shifts acidity. This is what ρ = 0.97 buys — and for a series of
  substituted pyridines or azoles the base calibration ranks even better (ρ = 1.00 in
  sample), so "which of these heterocycles is the stronger base" is a question this
  answers well.
- **Sanity-checking a literature or ELN value.** A predicted value two units from a
  reported one is within noise; five units apart means one of them is about a different
  species (or a different site — see the amphoteric trap below).
- **Direction-of-change arguments.** "Adding the *para*-nitro group should drop the pKa
  by roughly two units, so the acid wash will behave differently" — a qualitative claim
  the prediction supports.
- **Flagging that ionization matters at all.** A predicted pKa near the process pH is a
  reason to think about speciation, whatever its exact value.

## The amphoteric trap

**Acid wins whenever both are present.** If the molecule has any O-H or S-H, the tool
returns that site's pKa and never mentions the basic nitrogen it also has. For an amino
acid, an aminophenol, or any zwitterion-capable API you get a true statement about one
site and a misleading answer to "what is the pKa of this compound" — the basic centre is
not evaluated, and there is no warning that it was skipped.

So: **look at the structure before you report the number.** If the molecule has both, say
which site the value belongs to and that the other was not computed, so the compound's
ionization behaviour is only half known.

**Do not construct a modified molecule to get at the other site.** Feeding the tool an
edited structure — the acid capped, the proton deleted — makes it answer about a
different compound and returns a number that looks like it belongs to this one. The
supported answer for an amphoteric molecule is the acid site plus an explicit statement
that the basic site needs a measurement or a literature value. If it can be zwitterionic,
add that the neutral-form model the predictor uses is the wrong physical picture for it.

## Working the common questions

**Extraction / wash pH.** Use the prediction to establish the *ordering* and the rough
regime — "this is a carboxylic acid, so a basic wash should move it into the aqueous
layer" — and then either cite a measured pKa or recommend measuring one before fixing a
setpoint. Retrieve precedent first (`find_notes`): an ELN procedure that already worked
on this compound class beats any prediction.

**Salt and counterion selection.** The ΔpKa rule needs both partners' values. The
counterion (an acid) and an aryl-nitrogen base are both computable — but each carries its
own ±1 to ±1.6, so a ΔpKa built from two predictions has an uncertainty of the same order
as the 2–3 unit threshold it is being compared against. Report it as a direction, never
as a pass/fail on salt stability. If the API is an aliphatic amine, one partner is simply
not available: say so rather than computing the counterion's pKa alone and implying the
pair has been assessed.

**Solubility.** `predict_solubility` is an **aqueous, neutral-species** model. It does
not know about ionization, so it cannot answer "how much more soluble is the salt" or
"what is the solubility at pH 4". Do not combine a predicted pKa with a predicted
solubility into a pH–solubility profile; that composition is not supported by either
tool and would look far more authoritative than it is.

## Presenting a predicted pKa

State the value with its uncertainty, name the **site** it belongs to, say it is a
GFN2-xTB estimate rather than a measurement, and give the ordering claim rather than the
absolute one wherever the question allows. For a base, say "pKa of the conjugate acid
(pKaH)" — quoting it as "the pKa" of an amine reads as an acid pKa and is off by orders
of magnitude in the wrong direction. If the user is heading toward a pH setpoint, a salt
form, or a specification, say explicitly that the prediction is not the right basis and
what is.
