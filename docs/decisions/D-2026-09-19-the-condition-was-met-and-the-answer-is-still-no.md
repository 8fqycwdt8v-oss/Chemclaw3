# D-2026-09-19-the-condition-was-met-and-the-answer-is-still-no — D-092's reopening condition fired in the sibling fleet, and the three capabilities it was written for split onto three different grounds: MACE is refused on its weights' licence, ANI-2x/AIMNet2 stays unbuilt for want of a caller, and retrosynthesis was never under that condition at all

**Status:** accepted · **Date:** 2026-09-19 · **Supersedes:** the two "researched and deliberately
not built" paragraphs of `D-092-process-analytical-development-capability-research` (its five
shipped additions stand untouched) · **Builds on:**
`D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision` (which found this trigger had fired
and left the answer unwritten),
`D-2026-08-26-semiempirical-is-the-whole-tier`,
`D-2026-08-16-the-physics-leaves-the-cache-stays`,
`D-2026-08-09-a-connector-we-do-not-run`,
`D-135-a-dataset-may-be-vendored-into-the-image-at-build`

## Context

`D-092` declined two capabilities — ML interatomic potentials (ANI-2x/TorchANI, MACE-OFF/MACE-MP)
and AiZynthFinder retrosynthesis — under one condition:

> *"Revisit only if a deployment vendors the weight files into the container image at build time as
> an explicit, reviewed infrastructure decision (D-089's own escalation path) — not as a quiet
> runtime fetch."*

`D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision` found that condition **met**, in
`Chemclaw3-mcp`, by a mechanism no reader of `D-092` was watching, and that nobody had noticed. It
recorded the finding and stopped there, deliberately: its subject was the shape of the record, not
the chemistry. This is the decision that finding leaves open.

The important thing about a met trigger is the thing that ADR says about it: **a trigger is not a
control. It makes a question askable and answers nothing.** Asked, the question does not come back
with one answer. It comes back with three, on three grounds, only one of which `D-092` ever stated
— and none of them is "build it".

## What was measured, and what is taken on trust

Read against the two trees and against live upstream sources on 2026-09-19. Where a fact came off a
third party's page rather than out of this family's tree, it is marked, because that is the class of
fact this record has been wrong about before.

### 1. The condition fired twice, and only one of the two bakes meets the standard `D-092` wrote

**Verified in the tree.** `Chemclaw3-mcp/servers/rxnpredict/Containerfile` fetches its checkpoint in
a throwaway build stage, through `servers/rxnpredict/scripts/fetch_models.py`, which refuses to run
without `MCP_EGRESS_ALLOW` (exit 2) and refuses any revision that is not a 40-hex commit SHA (exit
2), *"because the checkpoint is loaded via torch.load"* — an unpickle of whoever last pushed. The
fetch runs with `MCP_EGRESS_GUARD=on`, so the allowlist is a restriction rather than a decoration.
The runtime stage sets `HF_HOME=/opt/models/hf`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and
re-arms the guard. Explicit, reviewed, build-time, pinned. That is `D-092`'s condition, exactly.

**The second bake is weaker than the summary of it, and the difference is worth stating rather than
rounding away.** `servers/rxnlabel/Containerfile` bakes RXNMapper's checkpoint by *constructing*
`RXNMapper()` in a `RUN` line. It is still build-time, still reviewed, still not a quiet runtime
fetch — so it satisfies the letter of the condition — but there is **no `MCP_EGRESS_ALLOW` on that
step, no SHA pin, and the guard is not armed for it**, because nothing in that `RUN` imports
`mcp_server_kit`. The install it rides on (`rxnmapper==0.4.3`, `rxn-insight==0.1.3`, from the CPU
torch index) is also the one install in that fleet outside `uv.lock`, which its own comment says.

So: the mechanism `D-092` asked for exists, is proven, and has a worked example whose script is
worth copying. It is **not** uniformly applied, and a future weight bake that copies the *second*
example gets build-time vendoring without the unpickle protection that made the first one
defensible.

### 2. MACE-OFF / MACE-MP — refused, on a ground `D-092` never stated

**Read from live upstream sources today, not from this tree.** `ACEsuit/mace`'s `LICENSE.md` is the
MIT licence, with no commercial restriction. The **weights** are a separate artifact under a
separate instrument: `ACEsuit/mace-off`'s README says, verbatim, *"By Downloading the models you
agree to the Academic Software License."* — the ACEsuit Academic Software Licence, free for academic
purposes and not for commercial ones. For a deployment serving pharmaceutical and chemical **process
R&D**, that disqualifies the weights outright, and it does so whatever the vendoring mechanism looks
like. `D-092`'s condition was about *how the bytes arrive*; this is about *whether they may be used
at all*, which the condition could never have answered.

**Nothing in this repository can catch that, and it is important to say why.**
`ingest/sources/vendored_dataset.py` declares `licence: str = Field(min_length=1)` — a required
field, satisfied by any non-empty string. `D-135` built the six-field manifest so a licence is
*recorded* and a person can read it in a pull request; it does not and cannot adjudicate one. The
refusal here is a human's, taken once, written down, and that is exactly what `D-135` intends.

Second, independent, and enough on its own in this tree: **`mace-torch` pins `e3nn==0.4.4`** — an
exact `==`, read off PyPI today for `mace-torch` 0.3.16. That is the same dependency-closure shape
`DEFERRED.md` already refuses for AiZynthFinder: an exact pin inside a shared resolution is a
constraint on everything else in it, and `e3nn` appears in neither repository's `uv.lock`.

### 3. ANI-2x / TorchANI and AIMNet2 — the stated blocker is gone; what replaces it is demand

**The blocker `D-092` named has genuinely lifted.** Its argument was entirely mechanical — *"fetches
its pretrained weights from the Hugging Face Hub on first use"* — and it makes **no licence claim at
all**, about ANI, about MACE, or about anything else. Read live today, `roitberg-group/ani2x` and
`isayevlab/aimnet2-*` (five checkpoints: `wb97m-d3`, `2025`, `nse`, `pd`, `rxn`) all carry
`license:mit` on the Hub, alongside MIT code. `torch` is already resolved at **2.13.0 in both
repositories' lockfiles**, so the heaviest transitive cost is already paid. And the bake pattern
that would deliver the weights is `fetch_models.py`, which already exists and already works. There
is no mechanical obstacle left.

**Four things stand in its place, and together they are decisive:**

- **Nothing asks for it.** Measured across the three registers where a want is recorded:
  `grep -ci` for `ani-2x|aimnet|mace|interatomic|torchani` returns **0** in
  `Chemclaw3/docs/planning/BACKLOG.md`, **0** in `Chemclaw3-mcp/docs/BACKLOG.md`, and **0** in
  `Chemclaw3-mcp/MODULES.md`. The only row anywhere is the `DEFERRED.md` row this ADR rewrites, and
  that row names its own substitute: the CREST/GFN2 ensemble, composed by
  `connectors/calc/compose.py`. An adopted capability with no caller is the shape a whole sweep was
  spent deleting.
- **The frame `D-092` argued from no longer exists.** It proposed an MLIP as a *"fast-ab-initio
  surrogate"*. `D-2026-08-26-semiempirical-is-the-whole-tier` deleted the `qm` bundle, the HPC
  launcher and `compute_dft_energy`; there is nothing left for it to be a surrogate *for*. The
  question it would now have to answer is a different one — "is GFN2 itself wrong here" — and the
  tree has a measured answer to that.
- **The accuracy gap this tree actually records is not on the gas-phase surface.** `predict_pka`
  refuses aliphatic amines: over 13 reference amines the computed basicity ranks at Spearman
  **−0.17**, no ranking ability at all, while aromatic and aryl nitrogen predicts at Spearman
  **1.000**, R² 0.993, RMSE 0.17. `connectors/calc/compose.py::_aryl_protonation` and
  `docs/guides/xtb-use-cases.md` both attribute it to the **continuum solvent**: gas-phase GFN2
  reproduces the experimental proton-affinity order exactly and ALPB reverses it, because the true
  aqueous order is set by the ammonium ion's hydrogen bonding to water. An MLIP trained on
  gas-phase reference energies inherits that unchanged. The capability that closes this gap is
  explicit-solvent or cluster-continuum treatment — CREST `--qcg`, already a `DEFERRED.md` row with
  this exact trigger — not a better potential-energy surface.
- **A second engine lands every prediction `UNCALIBRATED`.** The calibration ledger is keyed
  `(calc_type, calc_version, input_hash)` (`science/calc/calibration.py`). A new engine is a new
  version and a new type, so it matches zero rows, and `calculator_trust` answers *"no measurement
  has yet been reconciled against a prediction of this calculator version. Its accuracy is unknown,
  not good."* — which is the honest answer and is also the whole cost: adopting a second calculator
  means re-earning a calibration, from measurements, before a chemist may quote it. That is not a
  reason never to do it. It is a reason not to do it for nobody.

### 4. Retrosynthesis — `D-092`'s condition never applied to it

`DEFERRED.md` moved this blocker on 2026-08-13, before the condition fired and independently of it:
*"its blocker is not the weights and not their licence, it is the dependency closure"*. Re-measured
today, `aizynthfinder` 4.4.1's own metadata against this repository's `uv.lock`:

| `aizynthfinder==4.4.1` requires | this tree resolves |
|---|---|
| `numpy<2.0.0` | **2.4.6** |
| `networkx<3.0,>=2.4` | **3.6.1** |
| `rdkit<2024.0.0,>=2023.9.1` | **2026.3.5** |
| `pandas<3.0.0,>=1.0.0` | **3.0.5** |

Four mutual exclusions, unchanged. **One figure had moved and is corrected here**: the row said
pandas 3.0.3, measured 2026-08-27; it is 3.0.5. (`python_requires` is `>=3.10,<3.13` and is *not* a
fifth exclusion today — `requires-python` here is `>=3.11` with no upper bound.) Vendoring an
artifact cannot install a package that cannot be installed, so the shape that row already carries —
a separately built image reached as a `url:` connector bundle under
`D-2026-08-09-a-connector-we-do-not-run`, vendoring its Zenodo assets under `D-135`'s rules and
carrying the CC-BY attribution — stands unchanged and is the only shape this can take.

### 5. What this repository says about these licences is archived prose, and one sentence of it is wrong

`D-092` itself makes **no licence claim**. Its entire argument is the fetch mechanism. The only
licence statements about ANI or MACE anywhere in this tree are in
`docs/archive/REVIEW-2026-08-13-external-synthesis-and-gap-analysis.md`, an archived external
review, which says in three places that `D-092` evaluated ANI-2x/TorchANI and MACE-OFF/MACE-MP
*"both rejected on license/fetch grounds"* and, at line 1057, *"both rejected on license grounds"*.
**That is a misreading of `D-092`, and it is corrected here.** The same review was separately
*right* about the substance — it names the Academic Software Licence and calls MACE *"not viable
unless deployment stays strictly non-commercial"* — which is the honest shape of this whole episode:
the correct fact sitting in an archived document, attributed to an ADR that never said it, with
nothing reconciling the two. The licence facts in §2 and §3 above were therefore read from the
upstream sources today rather than taken from this tree, and they are dated for that reason.

## Decision

**1. MACE-OFF and MACE-MP are declined, permanently, on the weights' licence rather than on how the
weights arrive.** The code being MIT does not carry the weights, and the weights are what is used.
No vendoring mechanism, however well reviewed, makes a non-commercial artifact usable in a
commercial pharmaceutical deployment.

**Revisit when:** the MACE-OFF / MACE-MP **weights** are relicensed for commercial use — i.e. when
`ACEsuit/mace-off`'s README no longer conditions the download on the Academic Software Licence, or
that licence is superseded by one permitting commercial use. Read on 2026-09-19. **This trigger is
deliberately not executable and cannot be made so**: it is a fact about a document in a repository
this family does not control, which no test here can open (the same argument
`Chemclaw3-mcp/CLAUDE.md` makes about port numbers in a checkout it cannot see). What it names
instead is the exact artifact to re-read and the date it was last read. The second ground —
`mace-torch`'s exact `e3nn==0.4.4` — is independent and would still have to be answered separately,
in the `url:`-connector shape §4 describes, since an exact pin cannot enter this resolution.

**2. ANI-2x / TorchANI and AIMNet2 are not built.** Not for the reason `D-092` gave, which has
lifted, and not on a licence, which is clean: for want of a caller, and because the one accuracy
gap this tree has actually measured is in the solvation model, which an MLIP inherits rather than
fixes.

**Revisit when:** *both* halves hold — someone asks (a `BACKLOG.md` row in either repository naming
the capability and the question it answers, per `D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit`),
**and** the residual error is attributable to the gas-phase surface rather than to the continuum
solvent.

**The second half is executable, and the file that would show it had fired is
`tests/test_calc_ensembles.py`.** The aliphatic-amine refusal is the tree's own statement that the
gap is solvation's;
`test_an_aliphatic_amine_is_warned_about_rather_than_quietly_reported` and
`tests/test_logd.py::test_an_amphoteric_molecule_is_refused_rather_than_treated_as_an_acid` are what
hold it. **The day the explicit-solvent work lands and that refusal is lifted, those tests go red**
— and at that moment the question "what is the residual error now, and is it the potential-energy
surface?" is being asked by the suite rather than by a sentence nobody reads. If a residual survives
a solvation fix, this ADR is the one to reopen. If the refusal is never lifted, nothing here has
changed and nothing should be built.

Where that happens, the instrument for the first half already exists: a `calculator_trust` read for
`pka` under the new `calc_version` that reports `UNCALIBRATED` is the recorded miss showing a second
engine has been adopted without a calibration behind it — which is the cost, stated in advance.

**3. Retrosynthesis keeps its existing deferral and its existing shape.** `D-092`'s condition never
governed it, and the record is corrected to say so. Its blocker is four mutually exclusive
dependency pins, re-measured above, and the shape it must take when it is built is a separate image
declared here as a `url:` connector.

**Revisit when:** route planning becomes a stated need **and** the pins are re-measured. That
measurement is one command against files in this tree, which is the executable form available for a
condition about a resolution:

```sh
uv run --frozen python -c "import tomllib,pathlib;d=tomllib.loads(pathlib.Path('uv.lock').read_text());print({p['name']:p['version'] for p in d['package'] if p['name'] in {'numpy','networkx','rdkit','pandas'}})"
```

Any of the four dropping back under `aizynthfinder`'s bound changes the arithmetic; none of them
doing so means the `url:`-connector shape is still the only one available.

## What this deliberately does not do

**It builds nothing.** No server, no connector, no bundle, no dependency. Three of the three answers
are "not now" or "not ever", on three different grounds, and the only artifacts this produces are
this file and a rewritten register.

**It does not reopen the no-DFT decision.** `D-2026-08-26-semiempirical-is-the-whole-tier` deleted
the heavy tier, and an MLIP is not literally caught by that rule's letter: it is neither DFT nor a
cluster, and it would run in a pod like everything else. That letter is not the point.
`Chemclaw3-mcp/CLAUDE.md` says restoring a heavy tier is **Chemclaw3's decision to take again, in an
ADR, before anything is built there** — and adopting a second, heavier potential-energy surface as a
mid-tier accuracy point between GFN2 and nothing is that decision in substance, whatever it weighs.
**This ADR is not that decision and must not be cited as one.** It answers a narrower question — did
`D-092`'s trigger firing change the answer — and finds that it did not. Anyone proposing an MLIP
tier is taking the semiempirical-is-the-whole-tier decision again, and owes its own ADR.

**It does not edit `D-092`, and `D-092`'s five shipped additions are untouched.** A merged ADR is
right about the moment it was written; what changes is that this one supersedes the two paragraphs
that declined, and says on what grounds each half now rests.

**It does not claim anyone will watch these triggers.** That is the finding this ADR is built on:
the condition fired, twice, in a repository nobody was watching it from, and was met by a mechanism
nobody had in mind. Two of the three triggers above are therefore anchored to a file in this tree
that reds or to a command that answers; the MACE one is not, and says so rather than dressing a
sentence up as a check.

## What keeps it true

- `tests/test_declines_carry_a_trigger.py::test_a_decline_states_what_would_reopen_it` is what
  requires the three `Revisit when:` lines above, and is the rule this ADR is the first real
  exercise of — the ADR that introduced it declines nothing and is its own `_EXEMPT` entry.
- `tests/test_calc_ensembles.py::test_an_aliphatic_amine_is_warned_about_rather_than_quietly_reported`
  and `tests/test_logd.py::test_an_amphoteric_molecule_is_refused_rather_than_treated_as_an_acid`
  hold the aliphatic-amine refusal, which is decision 2's executable half: they are what go red the
  day the solvation gap is closed and the question this ADR answers "no" to has to be asked again.
- `tests/test_calc_remote.py::test_no_module_here_derives_a_calc_version` and
  `tests/test_calc_tools.py::test_predict_solubility_logs_the_version_the_result_was_computed_under`
  hold the `(calc_type, calc_version, input_hash)` identity that makes a second engine's every
  prediction `UNCALIBRATED` — the stated cost in decision 2, asserted rather than asserted about.
- `tests/test_deferred_register.py::test_no_row_is_struck_through` and
  `::test_every_row_states_a_trigger` hold the register rewrite this ADR lands with: the closed row
  is deleted rather than annotated, and each surviving row still names what would reopen it.
- `tests/test_decision_log.py::test_every_test_an_adr_names_still_exists` resolves every name in
  this list against the suite, so a rename cannot retire one of these citations in silence.
