# core kernel — correctness · reproduction verdicts

Lens: does it actually reproduce? Everything below was re-derived from source with my own scripts
(`/tmp/repro_stereo.py`, `/tmp/repro_downstream.py`, `/tmp/repro_sub.py`, `/tmp/repro_survey.py`);
the reporter's scripts were not run. RDKit 2026.03.5 as installed, `uv run` in this checkout.
`src/chemclaw/core/chem.py` is byte-identical to the pristine `HEAD` copy (`diff` → IDENTICAL), so
no mutation experiment is in play.

---

## `standardize` silently erases sp3 stereochemistry — enantiomers collapse to one compound id, one fingerprint row, one note

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical

### What I did

**1. Settled the mechanism against RDKit itself, not against the code's prose.**

```
$ uv run python /tmp/repro_stereo.py
rdkit version: 2026.03.5
STANDARDIZATION_VERSION: std4
default GetRemoveSp3Stereo: True
default GetRemoveBondStereo: True
```

`rdMolStandardize.TautomerEnumerator()` — the primary source — defaults to `removeSp3Stereo=True`
and `removeBondStereo=True`. `chem.py:120` constructs it with no configuration and `standardize`
(`chem.py:198–208`) calls `.Canonicalize` unconditionally, outside the `_identity_survives_stripping`
guard. Line numbers and symbols in the finding are real and current.

Stage-by-stage on (S)-naproxen (CAS 22204-53-1), my run:

```
  input           COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Cleanup   COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Parent    COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Uncharger COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Tautomer  COc1ccc2cc(C(C)C(=O)O)ccc2c1
  standardize()   COc1ccc2cc(C(C)C(=O)O)ccc2c1
  Tautomer w/ RemoveSp3Stereo=False: COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
```

Only stage 4 loses the `@`, and the one-line configuration the finding proposes does prevent it.

**2. Enantiomer pairs through the real helpers** (my own SMILES, built from CAS-identified pairs):

```
pair                   canon differ std differ same compound_id
alanine  L/D           True         False      True     std=CC(N)C(=O)O
ibuprofen S/R          True         False      True     std=CC(C)Cc1ccc(C(C)C(=O)O)cc1
naproxen S/R           True         False      True     std=COc1ccc2cc(C(C)C(=O)O)ccc2c1
thalidomide R/S        True         False      True     std=O=C1CCC(N2C(=O)c3ccccc3C2=O)C(=O)N1
2-butanol S/R          True         True       False    std=CC[C@H](C)O
1-phenylethanol S/R    True         True       False    std=C[C@H](O)c1ccccc1
propranolol S/R        True         True       False    std=CC(C)NC[C@H](O)COc1cccc2ccccc12
```

Matches the finding's table on every pair it lists, including the 2-butanol negative control.

**3. Drove the real downstream consumers** (`/tmp/repro_downstream.py`):

```
compound_id   compound-d7a9036fa36a compound-d7a9036fa36a True
ECFP4 equal?  True definition: ecfp:r2:b2048:std4
note id equal? True
note body equal? True
note body (S): 'Compound `COc1ccc2cc(C(C)C(=O)O)ccc2c1`.\n\n'
note compound_smiles (S): COc1ccc2cc(C(C)C(=O)O)ccc2c1

thalidomide compound_id equal? True compound-6511cdfe57e8
thalidomide ECFP equal? True
```

I got the reporter's exact hash `compound-d7a9036fa36a` from my own script. The KG compound note
(`ingest/eln/compound.compound_note`) writes the **achiral** structure into both its `body` and its
`compound_smiles` field.

**4. `memory.chains` — the causal-edge claim, driven end to end** through `detect_chains` with two
real `OrdReaction` records, one *producing* (S)-naproxen and one *consuming* (R)-naproxen:

```
chains: products(r1)  = {'COc1ccc2cc(C(C)C(=O)O)ccc2c1'}
chains: reactants(r2) = {'COc1ccc2cc(C(C)C(=O)O)ccc2c1'}
chains: spurious edge? True
chains detected: [(['rxn-makes-S', 'rxn-consumes-R'],
                   [{'from_reaction': 'rxn-makes-S', 'to_reaction': 'rxn-consumes-R',
                     'via_compound': 'COc1ccc2cc(C(C)C(=O)O)ccc2c1'}])]
```

A fabricated product→reactant campaign edge between two runs that never shared a compound. The
reporter asserted this; I confirmed it by running it.

**5. Traced whether anything downstream re-separates them. Nothing does.**
`ingest/eln/ingest.py:53` indexes `record_for(smiles, smiles)` where `smiles` is *already*
`standard_smiles(...)`, and `search.record_for` sets `id`, `label` **and** `bits` from that one
string — so the molecule corpus holds no stereo at all, and `find_substructure_matches`, which
re-matches the *stored* label with RDKit, has nothing chiral left to match against. The reporter's
grep result holds: `grep -niE "stereo|enantiom|chiral"` over `core/chem.py`,
`science/fingerprints/`, `ingest/eln/compound.py` and `tests/test_compound_identity.py` returns
**zero** hits (I ran it), and no test in that 478-line file asserts the collapse is intended.

**6. Two things that make it worse than filed.**

*Scope.* A survey of 20 chiral drugs/reagents (`/tmp/repro_survey.py`, counting assigned
stereocentres before and after with `Chem.FindMolChiralCenters`):

```
STEREOCENTRES LOST: L-alanine (1->0), L-phenylalanine (1->0), L-proline (1->0),
  L-cysteine (1->0), Ala-Gly dipeptide (1->0), (S)-ibuprofen (1->0), (S)-naproxen (1->0),
  (S)-ketoprofen (1->0), (S)-thalidomide (1->0), L-ascorbic acid (2->1), L-DOPA (1->0)
preserved: warfarin, salbutamol, propranolol, ketamine, 2-butanol, pseudoephedrine,
  menthol, D-glucose, atenolol
11/20 lose at least one assigned stereocentre
```

Every proteinogenic amino acid and every peptide bearing one loses its configuration — the finding
named amino acids but did not show that a *dipeptide* loses it too, i.e. the corpus's whole peptide
class is achiral to this index.

*The reaction fingerprint collapses too.* The finding stops at `molfp`. `rxnfp` also runs
`standard_smiles` per species (`rxnfp/fingerprint.py:41`, and `OrdReaction.transformation_smiles`
does the same at ingest), so an asymmetric synthesis is indistinguishable from its mirror image at
the reaction level as well:

```
asymmetric-hydrogenation DRFP equal? True     # prochiral acrylate >> (S)- vs (R)-naproxen
```

`similar_reactions` therefore scores an enantioselective route and its opposite-handed twin at 1.0.

### Why

Every link in the chain reproduces independently: RDKit's own default → the un-configured
enumerator at `chem.py:120` → the unguarded `.Canonicalize` at `chem.py:208` → identical
`compound_id`, identical ECFP4 bits, identical note id *and* body, a fabricated chain edge, and an
identical reaction fingerprint. The loss is silent — no exception, no log line, no metric, no test,
and not one occurrence of the word "stereo" anywhere in the module or its test file.

The only defence available to the code is that this is deliberate compound-level normalization, and
it does not survive contact with the source: `standard_smiles` is documented as answering "is this
the same compound?" with the examples *hydrochloride/free base* and *tautomer pair*. Two
enantiomers are not two spellings of one substance — dexibuprofen and (R)-ibuprofen have separate
CAS numbers, and the thalidomide pair is the canonical example in the discipline of why. The
`_identity_survives_stripping` gate proves the authors knew normalization can delete rather than
normalize; this is a fourth way it does so, arriving from the one stage that has no gate.

One honest narrowing, which does not change the verdict: `canonical_smiles` /
`require_canonical_smiles` — the calculation-cache and QM-workflow key — **do** preserve stereo
(measured: `canon differ = True` for all seven pairs), so a QM job on (S) is not served an (R)
result. And a *reaction* note's body renders `reaction_smiles()`, which is raw text and keeps the
`@` as display. Neither re-separates identity: the compound note, the compound id, the molecule
fingerprint row, the reaction fingerprint row and the chain graph are all achiral, and only the
identity paths are what the finding claims.

Critical stands. The only thing I would add to the fix is that bumping `STANDARDIZATION_VERSION`
will break `tests/test_compound_identity.py::test_the_derivation_is_pinned_to_a_literal`, which
pins a `compound-…` literal — that test is the intended tripwire, so update it in the same commit
rather than treating the failure as a surprise.

---

## `substructure_pattern` skips the whole-string gate `require_molecule` exists to be — a query with a space silently searches for its first fragment

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

`chem.py:339` is `pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)` — a bare parse,
nine lines below `require_molecule` (`chem.py:232`) and not calling it. My own run
(`/tmp/repro_sub.py`), printing the compiled pattern back out as SMARTS:

```
  'c1ccccc1 C(=O)Cl'           -> pattern 'c1ccccc1' (6 atoms)
  'c1ccccc1 (benzene)'         -> pattern 'c1ccccc1' (6 atoms)
  '°C'                         -> pattern 'C' (1 atoms)
  'CC(=O)N\tamide'             -> pattern 'CC(=O)N' (4 atoms)
  'CCO junk'                   -> pattern 'CCO' (3 atoms)
  ''  -> raised empty substructure query (no atoms): ''

matching a truncated query:
  c1ccccc1             matches? True
  Cc1ccccc1            matches? True
  O=C(Cl)c1ccccc1      matches? True
  CCO                  matches? False
  '°C' pattern matches: ['CCO', 'CC(=O)O']
```

So the acyl-chloride half is discarded and the search runs on bare benzene, returning toluene as a
"match" for an acyl chloride query, with no error and no signal. The zero-atom guard the docstring
advertises is real (`''` raises), which is exactly what makes the missing whitespace guard read as
deliberate.

**Reachability, checked rather than assumed.** Both callers are real and current:
`science/fingerprints/molfp/search.py:149`, reached from the registered agent tool
`substructure_matches` (`connectors/molfp/server/tools.py:53`, listed twice in
`connectors/molfp/connector.yaml:25,29`); and `connectors/calc/server/tools.py:616`
(`_only_matching`, the calibration outlier listing). I read the whole path in `search.py` for an
upstream gate and there is none — the only pre-parse check is
`settings.substructure_query_max_length`, a *length* cap, which a whitespace query passes. The
`InvalidSmilesError` → `FingerprintError` re-raise below it only relabels an error that is never
raised for this input.

**No signal on the way out.** The result payload's truncation flags (`scan_truncated`,
`hits_truncated`) describe the record cap and the top-k cap; nothing reports that the *query* was
truncated, so the model writing the answer has no way to know it searched for something else.

### Why

The mechanism, the arguments and the consequence all reproduce exactly as filed, on code that is
current, from two live callers, with no upstream mitigation. The failure direction is a superset —
the compiled prefix's match set contains the full query's — so the tool reports structures that do
*not* bear the group asked for, labelled as matches. That is a wrong scientific answer handed to a
chemist by an agent tool, and unlike the stereo defect the corpus itself is not corrupted, which is
why high rather than critical is right.

Two small exaggerations that do not change the verdict, recorded for accuracy:

- `"°C"` compiles via `MolFromSmarts` to **aliphatic** carbon, not "any carbon" — it matched
  `CCO` and `CC(=O)O` but *not* `c1ccccc1` in my run. "Matches essentially the whole corpus" is
  true of a typical organic corpus but false as literally stated; a purely aromatic molecule is
  not returned.
- The finding says the query narrows to "its first fragment"; precisely, it narrows to the
  whitespace-delimited *prefix*, which for a SMARTS need not be a chemically meaningful fragment.

The proposed fix is safe: a valid SMARTS or SMILES contains neither whitespace nor non-ASCII, so
the shared `_require_printable_ascii_token` helper rejects nothing legitimate, and factoring it out
of `require_molecule` is the right shape — the two guards drifting apart is how this one went
missing.
