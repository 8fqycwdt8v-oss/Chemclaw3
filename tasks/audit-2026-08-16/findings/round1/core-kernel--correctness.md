# core kernel — correctness

Slice: `src/chemclaw/core/config/*.py`, `core/errors.py`, `core/ids.py`, `core/bounded.py`,
`core/chem.py`, `core/reagents.py`. Lens: correctness (wrong answers, crashes, lost or silently
dropped data).

Everything below was reproduced by running code in this checkout (`uv run`, RDKit as installed).
Script output is quoted verbatim.

Two claims in the slice I *checked and found true*, so nobody re-spends the time:
`STANDARDIZATION_VERSION` really is folded into both fingerprint definitions
(`molfp/fingerprint.py:72`, `rxnfp/fingerprint.py:85`); and `errors.py`'s claim that every concrete
`ChemclawError` subclass is registered in `durable/publish._BAD_DATA_TYPES` holds — I enumerated all
31 subclasses by walking the tree and all 31 are listed, with `SubsystemUnavailableError` absent as
documented. `bounded.py` behaves exactly as its docstring says; I found nothing in it.

---

## `standardize` silently erases sp3 stereochemistry — enantiomers collapse to one compound id, one fingerprint row, one note

- **Severity**: critical
- **Location**: `src/chemclaw/core/chem.py:198` (`standardize`, the `_TAUTOMERS.Canonicalize` step);
  reached by `standard_smiles`, `require_standard_smiles`, `compound_id` (`chem.py:361`),
  `science/fingerprints/molfp/fingerprint.ecfp_bitstring:53`, `ingest/eln/compound.compound_note:47`,
  `memory/chains.py:46,51`, `memory/progression.py:180`.
- **Trigger**: any molecule with an sp3 stereocentre that sits in a tautomerisable position — i.e.
  alpha to a carbonyl, which covers alpha-aryl propionic acids (profens), all alpha-amino acids, and
  the glutarimide of thalidomide. Ingest `(S)-naproxen` `COc1ccc2cc(ccc2c1)[C@H](C)C(O)=O` and
  `(R)-naproxen` `COc1ccc2cc(ccc2c1)[C@@H](C)C(O)=O` as two ELN compounds.
- **Consequence**: the two enantiomers become the *same* compound everywhere the "same compound"
  key is used. Same `compound_id`, so one knowledge-graph note serves both and the second
  proposal is a no-op re-propose of the first. The note body records the **achiral** structure as
  the compound's structure. Same ECFP4 bitstring, so similarity search reports the wrong enantiomer
  as an exact structural neighbour and a chemist reading that hit cannot tell it from the molecule
  they asked about. `memory.chains` will build a causal product→reactant edge from a run that made
  one enantiomer to a run that consumed the other; `memory.progression` groups them as one species.
  For a process-chemistry group doing asymmetric synthesis this is the single most load-bearing
  distinction in the corpus, and it is deleted without a warning, a metric or a test.
- **Evidence**: RDKit's `TautomerEnumerator` defaults to `removeSp3Stereo = True`, and
  `chem.py:120` constructs it with no configuration:

  ```
  _TAUTOMERS = rdMolStandardize.TautomerEnumerator()
  ```

  Stage-by-stage on (S)-naproxen — only the tautomer step loses the `@`:

  ```
  input            COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Cleanup    COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Parent     COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Uncharger  COc1ccc2cc([C@H](C)C(=O)O)ccc2c1
  after Tautomer   COc1ccc2cc(C(C)C(=O)O)ccc2c1
  GetRemoveSp3Stereo: True   GetRemoveBondStereo: True
  ```

  Across six real drug/reagent pairs (`canonical_smiles` keeps the stereo; `standard_smiles`
  does not):

  ```
  L-alanine / D-alanine            canonical differ? True   standardized differ? False   same compound_id? True
  (S)/(R)-ibuprofen                canonical differ? True   standardized differ? False   same compound_id? True
  (S)/(R)-2-butanol                canonical differ? True   standardized differ? True    same compound_id? False
  L-/D-phenylalanine               canonical differ? True   standardized differ? False   same compound_id? True
  (S)/(R)-naproxen                 canonical differ? True   standardized differ? False   same compound_id? True
  (R)/(S)-thalidomide              canonical differ? True   standardized differ? False   same compound_id? True
  ```

  (2-butanol survives only because its stereocentre is not alpha to a carbonyl — so this is not a
  blanket loss, it is a loss exactly where pharma cares.)

  Driving the real downstream consumers:

  ```
  compound_id equal?  True compound-d7a9036fa36a
  ECFP4 row equal?    True
  note id equal?      True
  note body equal?    True
  note body (S): 'Compound `COc1ccc2cc(C(C)C(=O)O)ccc2c1`.\n\n'
  ```

  The module docstring enumerates exactly three ways `FragmentParent`/`Uncharger` can "delete the
  reagent rather than normalize it" and builds `_identity_survives_stripping` to hold them off.
  Stereo destruction is a fourth, it comes from a different pipeline stage that has no guard at all,
  and it is not mentioned anywhere: `grep -i "stereo|enantiom|chiral"` over `core/chem.py`,
  `science/fingerprints/`, `ingest/eln/compound.py` and `tests/test_compound_identity.py` returns
  **zero** hits. The docstring's stated purpose — "two spellings of one molecule collapse to one
  string" — is violated: an enantiomer is not a spelling.
- **Fix**: configure the enumerator to preserve stereo and bump the standardization version so
  existing rows fall out of similarity search rather than being compared under two notions of
  identity:

  ```python
  _TAUTOMERS = rdMolStandardize.TautomerEnumerator()
  _TAUTOMERS.SetRemoveSp3Stereo(False)
  _TAUTOMERS.SetRemoveBondStereo(False)   # E/Z survives today only by accident of which bonds tautomerise
  STANDARDIZATION_VERSION = "std5"
  ```

  and add a test asserting `compound_id(S) != compound_id(R)` for at least the naproxen and
  thalidomide pairs. If merging enantiomers is ever *wanted* it must be a second, separately-named
  key ("same constitution?"), not the one `compound_id` is built on.

---

## `substructure_pattern` skips the whole-string gate `require_molecule` exists to be — a query with a space silently searches for its first fragment

- **Severity**: high
- **Location**: `src/chemclaw/core/chem.py:339` (`substructure_pattern`); callers
  `science/fingerprints/molfp/search.py:149` (the `find_substructure_matches` agent tool) and
  `connectors/calc/server/tools.py:616` (the calibration outlier listing).
- **Trigger**: any query string containing whitespace or a leading/trailing non-ASCII character —
  a two-fragment query, a pasted label, a model-generated SMARTS with a trailing comment.
  `substructure_pattern("c1ccccc1 C(=O)Cl")`.
- **Consequence**: the acyl chloride is discarded and the search runs on bare benzene, returning a
  *superset* of what was asked for, labelled as matches for the query the caller typed. There is no
  error, no warning and no truncation signal. `"°C"` — which the module's own `require_molecule`
  docstring names as the exact prose-derived hazard — compiles to the one-atom pattern `C`, which
  matches essentially the whole corpus up to `substructure_scan_max_records` (5000).
- **Evidence**:

  ```
  'c1ccccc1 C(=O)Cl'                   -> pattern 'c1ccccc1' (6 atoms)
  'c1ccccc1 (benzene)'                 -> pattern 'c1ccccc1' (6 atoms)
  '°C'                                 -> pattern 'C' (1 atoms)
  'CC(=O)N\tamide'                     -> pattern 'CC(=O)N' (4 atoms)
    toluene-like c1ccccc1        matches truncated query? True
    toluene-like Cc1ccccc1       matches truncated query? True
    toluene-like O=C(Cl)c1ccccc1 matches truncated query? True
  ```

  The code is `pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)` — a bare parse.
  `require_molecule` (`chem.py:232`) was written specifically to reject "a string with embedded
  whitespace… the whole silent-truncation class: a malformed or concatenated string does not fail,
  it narrows to a *different, smaller molecule* than the caller submitted", and its docstring cites
  the hazard screen as the bug this caused. `substructure_pattern` sits nine lines below it and does
  not use it. The docstring's *other* guard ("A zero-atom pattern is rejected rather than run") is
  real — I confirmed `substructure_pattern("")` raises — which makes the missing one look
  deliberate when it is not.
- **Fix**: apply the same string gate before parsing:

  ```python
  stripped = query.strip()
  if not stripped or any(ch.isspace() for ch in stripped) or not stripped.isascii():
      raise InvalidSmilesError(f"invalid substructure query (whitespace or non-ASCII): {query!r}")
  pattern = Chem.MolFromSmarts(stripped) or Chem.MolFromSmiles(stripped)
  ```

  Best factored as a shared `_require_printable_ascii_token(s)` used by both `require_molecule` and
  `substructure_pattern`, so the two cannot drift.

---

## The lenient `canonical_smiles` / `standard_smiles` truncate at whitespace instead of returning the input, contradicting their own docstrings

- **Severity**: medium
- **Location**: `src/chemclaw/core/chem.py:219` (`canonical_smiles`) and `chem.py:289`
  (`standard_smiles`, via `_standardized:132`).
- **Trigger**: a SMILES field carrying a trailing token — an ELN export that concatenates structure
  and label (`"CCO ethanol"`), a tab-separated field pasted whole, or a code span lifted from prose
  (`"°C"`). Ingestion reaches these at `ingest/eln/ingest.py:53,78`, `ingest/eln/ord.py:292-293`,
  `memory/chains.py:46,51`, `memory/progression.py:180`,
  `connectors/calc/server/tools.py:115,153`.
- **Consequence**: the record is keyed to a **different, smaller molecule** rather than to the odd
  string. `"CCO junk"` is indexed as ethanol; `"°C"` is indexed as methane; `"CC O"` as ethane. The
  fingerprint row, the compound note and the product↔reactant match key all describe a molecule the
  ELN never recorded — and `record_prediction` (`connectors/calc/server/tools.py:115`) writes it as
  the calibration ledger's `subject`, so a later measurement is reconciled against a prediction for
  the wrong compound.
- **Evidence**: both docstrings promise "or the input unchanged if it does not parse" and justify
  leniency on the grounds that "the ELN/memory callers key on whatever string they are given". The
  measured behaviour is that these inputs *do* parse — to something else:

  ```
  canonical_smiles('CCO junk')   -> 'CCO'
  canonical_smiles('CCO\tCCCC')  -> 'CCO'
  standard_smiles('CCO junk')    -> 'CCO'
  canonical_smiles('°C')         -> 'C'
  standard_smiles('°C')          -> 'C'
  'CC O'  mol=2  canonical='CC'  standard='CC'
  ```

  Note also that `''` parses to a zero-atom molecule, so `standard_smiles('') == ''` — a valid
  dictionary key for nothing.
  The module's own `require_molecule` docstring diagnoses this precisely and then applies the fix
  only on the strict path. The leniency the lenient pair was designed for is "don't abort a batch on
  one odd label"; silently substituting a smaller molecule is a different behaviour and is not what
  the docstring describes.
- **Fix**: keep leniency but make it honest — run the same string gate first and return the input
  unchanged when it fails, so leniency means "pass the string through", not "parse a prefix":

  ```python
  def canonical_smiles(smiles: str) -> str:
      try:
          return require_canonical_smiles(smiles)
      except InvalidSmilesError:
          return smiles
  ```

  (same shape for `standard_smiles`). That makes both docstrings true and costs nothing, since the
  strict helper already exists.

---

## `resolve_compound_name`'s SMILES fallback fabricates a structure for names that happen to be valid SMILES

- **Severity**: medium
- **Location**: `src/chemclaw/core/reagents.py:330-339` (`resolve_compound_name`); reached from
  `ingest/eln/ord_adapter.py:333` (`_structure`, the `NAME`/`IUPAC_NAME` branch) and
  `memory/optimization.py:70` (`canonical_condition`).
- **Trigger**: an ORD/ELN compound whose only identifier is a `NAME` that is also parseable as
  SMILES. The canonical case is `"CO"` — carbon monoxide to every chemist, methanol to RDKit.
- **Consequence**: the resolver returns a *confident, named* wrong structure. The module docstring's
  central invariant — "Resolution is deliberately *conservative*: an unknown name returns no match
  rather than a guess. Fabricating a structure from a name is the one failure mode that would be
  worse than the gap" — does not hold on this path, and `ord_adapter._structure`'s own docstring
  ("Refusing to invent a structure is the point (a fabricated one propagates silently into a
  fingerprint index, a similarity hit and eventually a proposed note)") is the code that consumes
  it. A carbonylation record then indexes methanol as a reactant, and `density_of("CO")` returns
  0.792 g/mL — a liquid density for a gas — into whatever stoichiometry/green-metric arithmetic
  asked.
- **Evidence**:

  ```
  'CO'      -> ('CO', 'methanol', 'smiles')   density=0.792
  'N'       -> ('N', 'N', 'smiles')           density=None
  'S'       -> ('S', 'S', 'smiles')           density=None
  'Br'      -> ('Br', 'Br', 'smiles')         density=None
  ```

  Only `"CO"` also picks up a display name and a density, because methanol is in `_RAW_SYNONYMS`;
  the others at least fall back to echoing the query. Nothing on `ResolvedCompound` distinguishes
  "the caller handed me a structure" from "I guessed that this name was a structure" other than
  `source="smiles"`, which neither consumer inspects.
- **Fix**: the SMILES fallback exists for callers that already hold a structure, and those callers
  know it. Split the two questions rather than guessing between them — either give
  `resolve_compound_name` an explicit `allow_smiles: bool = False` and have the two structure-holding
  call sites opt in, or refuse the fallback for inputs that look like a name rather than a structure.
  A cheap, adequate narrowing: reject the fallback when the query has no character outside
  `[A-Za-z]` and is at most three characters (`CO`, `NO`, `CC`, `Br`), which is exactly the band
  where element-symbol shorthand and SMILES collide.

---

## `stable_hash`'s `default=str` turns a non-JSON payload into a *process-dependent* identity instead of an error

- **Severity**: medium
- **Location**: `src/chemclaw/core/ids.py:35` (`stable_hash`).
- **Trigger**: any caller passing a value `json` cannot serialize natively. `stable_hash` accepts
  `payload: Any` and two of its consumers are typed `Any` all the way down —
  `science/calc/store.CalculationKey.build(inputs: Any, params: Any = None)` and
  `connectors/jobs.job_workflow_id(payload: dict[str, Any])`, the latter being *the durable-job
  idempotency key*. A `set`, `frozenset` or plain object anywhere in that payload is accepted
  silently.
- **Consequence**: the "stable" hash is not stable across processes. A `set` is stringified through
  `str()`, whose element order depends on `PYTHONHASHSEED`, so two pods derive two different
  identities for the same input — which for `job_workflow_id` means two durable workflows for one
  logical job (D-011's "compute once" broken with no symptom) and for `CalculationKey` means a cache
  that never hits. A plain object's `str()` embeds its memory address, so the hash differs on every
  run of the same process. Without `default=str`, all of these would be a loud `TypeError` at the
  call site.
- **Evidence** (same script, three hash seeds):

  ```
  --- PYTHONHASHSEED=0
  set: 5e4be7ae7d20fb36        opaque object: f75b690cf46cb212
  --- PYTHONHASHSEED=1
  set: 2a7bbd63e0e6272c        opaque object: b6148f159bbb4941
  --- PYTHONHASHSEED=2
  set: 0adc019ef41c84c3        opaque object: 6082c657545475d3
  tuple vs list:      036b898f9248c0e8 == 036b898f9248c0e8   (collide)
  int key vs str key: 13f79ea251e19f42 == 13f79ea251e19f42   (collide)
  float 3.5 vs "3.5": 27bb4ef9ae664d31 != f1079f1b3b44b0d6   (fork — a numpy scalar hashes
                                                              differently from the same float)
  ```

  The docstring asserts the opposite: "`default=str` lets values that are not JSON-native serialize
  deterministically." That is false for the two most likely non-native types. In honesty: I traced
  every current call site and none demonstrably passes a `set` today — `prepare_job_launch` runs
  params through a pydantic model first, and the calc keys pass plain dicts. This is a latent defect
  in a function whose whole contract is determinism, not an active one.
- **Fix**: drop `default=str` and let the `TypeError` fire at the caller, or replace it with a
  narrow, explicitly deterministic coercion (sorted for sets, `.isoformat()` for datetimes) that
  raises on anything else. Also correct the docstring: `default=str` is what makes an out-of-contract
  input *silent*, not what makes it deterministic.

---

## `CalculatorSettings`' "None of them enters a cache key" is false for `xtb_geometry_decimals`, and the paragraph invites the change that breaks the cache

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/calculators.py:32-37` (class docstring) and
  `calculators.py:54-62`; contradicted by `science/calc/models.py:111` and `models.py:147`
  (`Structure.structure_id`).
- **Trigger**: an operator reads "**None of them enters a cache key** … so changing anything here
  invalidates nothing and recomputes nothing", and sets `CHEMCLAW_XTB_GEOMETRY_DECIMALS` on this
  deployment (e.g. to 5, chasing precision) without making the identical change on the
  `Chemclaw3-mcp` calc server.
- **Consequence**: `Structure._normalize_and_validate` rounds coordinates to that many decimals and
  `structure_id` hashes the rounded positions, so every structure this repository builds is now
  addressed differently from the server's. `Structure`'s own docstring states the consequence
  exactly: "a divergence would not raise anywhere: every lookup would simply miss, forever." Every
  cached Hessian, optimized geometry and conformer ensemble becomes permanently unreachable and
  every calculation is recomputed — the precise failure D-011 exists to prevent — with no error
  anywhere and a config docstring saying it cannot happen.
- **Evidence**: `models.py:111` `decimals = settings.xtb_geometry_decimals` feeds the rounding whose
  output `structure_id` hashes. Measured on one three-atom structure:

  ```
  decimals = 4 -> structure_id st_27ab4fe55f5a9638
  decimals = 5 -> structure_id st_af22fc51b5765b42
  same? False
  ```

  The file also contradicts itself in place: the field's own comment nine lines below the class
  docstring says "it is part of the structure id, so changing it re-addresses every structure and
  therefore recomputes." One of the two is wrong and the class-level one is the one an operator
  reads first. Separately, the comment block at `calculators.py:54-56` documents
  **`xtb_embed_seed`** ("it is part of the cache key so changing it recomputes") — a field that does
  not exist anywhere in the settings class or the tree.
- **Fix**: correct the class docstring to say that the *calculator* knobs no longer enter a key but
  that `xtb_geometry_decimals` still does, and that it is a **cross-repository** value which must be
  changed on both sides together or not at all; delete the dead `xtb_embed_seed` sentence. Better
  still, pin it: `tests/` already asserts a shared `st_` hash for `CCO` against the server (per the
  `Structure` docstring) — make that test read the setting so a unilateral change fails CI rather
  than silently draining the cache.
