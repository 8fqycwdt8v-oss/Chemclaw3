---
name: ionization-and-partitioning
description: >-
  Judgment for using a predicted pKa in process decisions — extraction and wash pH, salt
  and counterion questions, ionization state — including the two things that make it
  dangerous: basic amines are out of scope, and individual values miss by up to two units.
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
(`tests/test_pka.py`): the *ordering* is reliable (Spearman ρ 0.965), individual values
miss by up to **2.1 units**, and the reported uncertainty is ±1.6. Process rules of thumb
— "extract two units below the pKa", "ΔpKa > 2–3 for a stable salt" — turn on exactly
that margin, so a plausible-looking prediction can invert the decision. If a pH, a salt
form, or a specification depends on the number, the answer is **"measure it"**, not a
computed value with a caveat attached.

**2. Basic amines are out of scope.** The predictor covers **neutral O-H/S-H acids only**
— carboxylic acids, phenols, alcohols, thiols. It has nothing to say about the
conjugate acid of an amine, and most APIs are basic amines. It errors out rather than
guessing, which is correct; your job is to say so plainly rather than reaching for a
different tool and presenting the result as a pKa.

## What it is genuinely good for

- **Ranking a congeneric series.** Which of six phenol analogues is most acidic; how a
  substituent shifts acidity. This is what ρ = 0.97 buys.
- **Sanity-checking a literature or ELN value.** A predicted value two units from a
  reported one is within noise; five units apart means one of them is about a different
  species (or a different site — see the amphoteric trap below).
- **Direction-of-change arguments.** "Adding the *para*-nitro group should drop the pKa
  by roughly two units, so the acid wash will behave differently" — a qualitative claim
  the prediction supports.
- **Flagging that ionization matters at all.** A predicted pKa near the process pH is a
  reason to think about speciation, whatever its exact value.

## The amphoteric trap

The tool returns the pKa of **the most acidic O-H/S-H site it can find** — it does not
know that the molecule also has a basic centre, and it will not tell you it ignored one.
For an amino acid, an aminophenol, or any zwitterion-capable API it reports the acid
site's value as "the pKa", which is a true statement about one site and a misleading
answer to "what is the pKa of this compound".

So: **look at the structure before you report the number.** If the molecule has a basic
nitrogen, say which site the value belongs to, say that the basic site is not covered,
and do not present a single number as the compound's ionization behaviour. If it can be
zwitterionic, note that the neutral-form model the predictor uses is the wrong physical
picture for it.

## Working the common questions

**Extraction / wash pH.** Use the prediction to establish the *ordering* and the rough
regime — "this is a carboxylic acid, so a basic wash should move it into the aqueous
layer" — and then either cite a measured pKa or recommend measuring one before fixing a
setpoint. Retrieve precedent first (`find_notes`): an ELN procedure that already worked
on this compound class beats any prediction.

**Salt and counterion selection.** The ΔpKa rule needs both partners' values, and the
API is usually the basic partner — which is out of scope. State that limit rather than
computing the counterion's pKa and implying the pair has been assessed.

**Solubility.** `predict_solubility` is an **aqueous, neutral-species** model. It does
not know about ionization, so it cannot answer "how much more soluble is the salt" or
"what is the solubility at pH 4". Do not combine a predicted pKa with a predicted
solubility into a pH–solubility profile; that composition is not supported by either
tool and would look far more authoritative than it is.

## Presenting a predicted pKa

State the value with its uncertainty, name the **site** it belongs to, say it is a
GFN2-xTB estimate rather than a measurement, and give the ordering claim rather than the
absolute one wherever the question allows. If the user is heading toward a pH setpoint,
a salt form, or a specification, say explicitly that the prediction is not the right
basis and what is.
