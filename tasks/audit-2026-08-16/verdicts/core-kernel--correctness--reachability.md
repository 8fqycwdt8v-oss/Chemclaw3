# core kernel — correctness · reachability/consequence verification

Working tree note: `src/chemclaw/core/chem.py` is byte-identical to the pristine `HEAD` copy
(`diff` against `.../scratchpad/pristine/src/chemclaw/core/chem.py` → IDENTICAL), so nothing below
is an artifact of another agent's mutation experiment. RDKit as installed: **2026.03.5**.

---

## `standardize` silently erases sp3 stereochemistry — enantiomers collapse to one compound id, one fingerprint row, one note

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical
- **What I did**:

  1. Reproduced the mechanism against RDKit itself, not against the code's description of it
     (`/tmp/verif/stereo.py`, `uv run python`):

     ```
     RDKit 2026.03.5 STD_VERSION std4
     RemoveSp3Stereo True RemoveBondStereo True
     L-alanine       /D-alanine        canon_differ=True std_differ=False same_id=True
     (S)-ibuprofen   /(R)-ibuprofen    canon_differ=True std_differ=False same_id=True
     (S)-2-butanol   /(R)-2-butanol    canon_differ=True std_differ=True  same_id=False
     L-phe           /D-phe            canon_differ=True std_differ=False same_id=True
     (S)-naproxen    /(R)-naproxen     canon_differ=True std_differ=False same_id=True
     (R)-thalidomide /(S)-thalidomide  canon_differ=True std_differ=False same_id=True
     ```

     Stage-by-stage on (S)-naproxen, only the tautomer step loses the `@`:

     ```
     input           COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
     after Cleanup   COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
     after Parent    COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
     after Uncharge  COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
     after Tautomer  COc1ccc2cc(C(C)C(=O)O)ccc2c1
     ```

  2. Drove the *real* downstream consumers, not stand-ins (`/tmp/verif/down.py`):

     ```
     compound_id equal? True compound-d7a9036fa36a
     ECFP4 equal? True
     note id equal? True   body equal? True
     note body (S): 'Compound `COc1ccc2cc(C(C)C(=O)O)ccc2c1`.\n\n'
     note compound_smiles: COc1ccc2cc(C(C)C(=O)O)ccc2c1
     record id S: COc1ccc2cc(C(C)C(=O)O)ccc2c1 == R: True   bits equal: True
     hit payload: {'compound_note_id': 'compound-d7a9036fa36a',
                   'smiles': 'COc1ccc2cc(C(C)C(=O)O)ccc2c1', 'similarity': 1.0}
     ```

  3. Produced the false causal edge the finding predicts, with the real
     `memory.chains.detect_chains` over two real `OrdReaction`s — run A *makes* (S)-naproxen,
     run B *consumes* (R)-naproxen (`/tmp/verif/chain.py`):

     ```
     chains: [(['A', 'B'], [('A', 'B', {'from_reaction': 'A', 'to_reaction': 'B',
                             'via_compound': 'COc1ccc2cc(C(C)C(=O)O)ccc2c1'})])]
     ```

     Two runs that share no substance are reported as one campaign, linked "via" a compound
     neither of them contains.

  4. Traced reachability back to the outermost entry point and looked for anything that stands in
     the way. Nothing does. `ingest/eln/ord_adapter._structure` returns the ELN's `SMILES`
     identifier **verbatim** (`ord_adapter.py:320-323`), so stereo survives into `OrdReaction`;
     `ingest/eln/sync.py:216` calls `ingest_reaction`, which is the Temporal ELN-sync path;
     `ingest.py:53` indexes `{standard_smiles(c.smiles) for c in reaction.compounds()}` and
     `record_for(smiles, smiles)` makes the row id *be* the standardized string. There is no
     pydantic constraint, validator, gate or config flag anywhere on that path that preserves or
     restores stereo — `Component.smiles` is `str = Field(min_length=1)` and nothing else.

  5. Looked for any downstream re-separation. There is none on any *key*. `grep -rni
     "stereo|enantiom|chiral"` over `core/chem.py`, `science/fingerprints/`, `ingest/eln/` and
     `tests/test_compound_identity.py` returns zero hits, and `grep -rli "enantiom|stereo" tests/`
     returns **no test file at all** — the behaviour is neither asserted nor forbidden anywhere.

- **Why**: mechanism, trigger and consequence all hold, and the trigger is not exotic — it is
  every alpha-amino acid and every profen, i.e. a large fraction of a pharma corpus. The one thing
  that could have refuted this — a stereo-aware key somewhere downstream — does not exist; the only
  place stereochemistry survives is *display* text (`reaction_smiles()` and the reaction note body
  echo raw component SMILES), which is precisely where it cannot re-separate a record.

  Two corrections in the finding's favour, i.e. it is if anything **understated**:

  - **The reaction fingerprint is stereo-blind too**, which the finding does not mention.
    `OrdReaction.transformation_smiles` (`ord.py:291-292`) builds the DRFP input from
    `standard_smiles` per species, so an asymmetric synthesis and its enantiomeric counterpart are
    one row (`/tmp/verif/rxn.py`):

    ```
    transformation A: C=Cc1ccc2cc(OC)ccc2c1>>COc1ccc2cc(C(C)C(=O)O)ccc2c1
    transformation B: C=Cc1ccc2cc(OC)ccc2c1>>COc1ccc2cc(C(C)C(=O)O)ccc2c1
    DRFP identical? True
    ```

    So `similar_reactions` scores an (S)-selective route against an (R)-selective one at 1.0 — the
    defect reaches the reaction retrieval layer, not only compound identity.
  - The damage is **persisted and version-invisible**: it lives in fingerprint rows and merged KG
    notes, and `STANDARDIZATION_VERSION` ("std4") is folded into `molecule_definition()`, so fixing
    this without bumping it would rank stereo-aware rows against stereo-blind ones. The finding's
    fix already says this; it is the reason the severity is not merely "wrong answer" but "corpus
    to re-index".

  One paraphrase I would soften without changing the verdict: "similarity search reports the
  **wrong enantiomer** as an exact structural neighbour" is not literally what the payload shows —
  the hit carries the *achiral* SMILES `COc1ccc2cc(C(C)C(=O)O)ccc2c1` at similarity 1.0, i.e. the
  stereocentre is absent rather than inverted. That is still a wrong answer (the record asserts an
  unspecified-configuration structure as *the* compound), but a reader sees a stereo-free SMILES,
  not a mislabelled `[C@@H]`.

  Note also that `canonical_smiles` — the calculation-cache / QM-dedup / prediction-ledger key —
  **does** keep stereo (measured above: `canon_differ=True` for all six pairs). The defect is
  confined to the "same compound?" axis, exactly as the finding scopes it. That containment is why
  it is critical-but-not-total, and it does not reduce the severity of the axis it does hit.

---

## `substructure_pattern` skips the whole-string gate `require_molecule` exists to be — a query with a space silently searches for its first fragment

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  1. Reproduced the parse behaviour exactly (`/tmp/verif/sub.py`):

     ```
     'c1ccccc1 C(=O)Cl'         -> 'c1ccccc1' atoms=6
     'c1ccccc1 (benzene)'       -> 'c1ccccc1' atoms=6
     '°C'                       -> 'C' atoms=1
     'CC(=O)N\tamide'           -> 'CC(=O)N' atoms=4
     '[#6]!@[OX2H] # alcohol'   -> '[#6]!@[O&X2&H1]' atoms=2
     'not a molecule at all )(' -> RAISED unparseable substructure query
     ''                         -> RAISED empty substructure query (no atoms)
     ```

  2. Ran the real agent tool path end to end over a 5-molecule in-memory index
     (`find_substructure_matches`, `/tmp/verif/sub2.py`):

     ```
     'c1ccccc1 C(=O)Cl' -> ['CC(=O)Oc1ccccc1C(=O)O','Cc1ccccc1','O=C(Cl)c1ccccc1','c1ccccc1']  trunc: False False
     '°C'               -> ['CC(=O)Oc1ccccc1C(=O)O','CCO','Cc1ccccc1','O=C(Cl)c1ccccc1']       trunc: False False
     'c1ccccc1C(=O)Cl'  -> ['O=C(Cl)c1ccccc1']                                                 trunc: False False
     ```

     Four hits where the intended query returns one, with `scan_truncated=False`,
     `hits_truncated=False` and no log line. So the "no error, no warning, no truncation signal"
     half of the consequence is exactly right.

  3. Checked reachability upstream. `connectors/molfp/server/tools.py:53`
     `async def substructure_matches(query: str)` — a bare `str`, declared in `connector.yaml`, no
     pattern/format constraint anywhere; the only upstream gate is
     `settings.substructure_query_max_length` (a length cap, `search.py:139`). The second caller,
     `connectors/calc/server/tools.py:616` (`_only_matching`), has no gate at all. **Reachable from
     a normal turn** — I do not dispute reachability.

- **Why**: the mechanism and the trigger hold; what does not hold is the "high" weight, on three
  measured grounds.

  - **The error is strictly over-inclusive, in every case, by construction.** Truncation always
    yields a *prefix* pattern, which is a less-constrained pattern, so the returned set is always a
    superset of the correct answer. I could not construct a truncation that drops a true hit or
    that matches a *different* molecule: a prefix that would change meaning (`"c1ccc cc1"`) fails
    both `MolFromSmarts` and `MolFromSmiles` and raises. That is materially different from the
    precedent the finding leans on: `require_molecule`'s docstring case was `screen_hazards("CCO
    junk")` returning a **clean screen** — a false negative on safety. Here the failure mode is a
    noisy positive, and the extra hits arrive with their own real SMILES in the payload, so the
    over-breadth is visible in the answer rather than hidden behind it.
  - **Nothing is persisted.** Both callers are read-only searches (the `molfp` bundle declares both
    tools `read_only`, and the connector cannot write the index at all). No row, note, cache key or
    ledger subject is derived from the truncated pattern — contrast finding 1, where the truncation
    analogue becomes a stored id.
  - **The trigger requires a malformed query.** Whitespace is not valid SMARTS syntax for anything;
    a disconnected multi-fragment query is written with `.`. And the `°C` case is not symmetric
    with the hazard-screen case it is compared to: a code span lifted from a note body is a
    plausible *structure* argument, but passing a temperature to a substructure search is not a
    normal flow. Note too that the most likely real "model-generated SMARTS with a trailing
    comment" case is **benign** — `'[#6]!@[OX2H] # alcohol'` compiles to the intended two-atom
    pattern, because the truncation lands after the whole query.

  The finding's factual claims are accurate and the proposed fix is cheap, correct and worth
  taking (a shared `_require_printable_ascii_token` used by both functions). But "high" should be
  reserved for a wrong answer that is not visibly wrong, or one that lands in a store; this is a
  visibly over-broad, transient search result produced by an ill-formed query. Medium.
