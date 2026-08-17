# `src/chemclaw/science/calc/` — design and simplification

Scope: `__init__.py`, `store.py`, `postgres_store.py`, `calibration.py`, `artifacts.py`,
`postgres_artifacts.py`, `models.py`, `thermo.py`, `logd.py`, `uncertainty.py`, `solvents.py`
(3,419 LOC). All eleven read in full. Findings are ordered by severity.

Nearly every structural defect below has the same shape: the physics moved out of this repository
and the *shells* it used to fill stayed. Five request models with no requester, an
applicability-domain check with no predictor, a media-type table for files nothing captures, a
17 MB compiled dependency with no importer, a refusal message naming calculators that no longer
exist under those names. None of it is broken; all of it is structure that outlived its buyer.

---

## Five request models in `models.py` have no reference anywhere in the repository

- **Severity**: high
- **Location**: `src/chemclaw/science/calc/models.py:160` (`XtbInput`), `:182` (`PkaInput`),
  `:212` (`SolubilityInput`), `:237` (`DescriptorInput`), `:266` (`LogdInput`)
- **Trigger**: Grep the whole tree — `src/`, `tests/`, `*.yaml`, `*.json`, `*.toml` — for each
  name. Every hit is the `class` statement itself, `docs/archive/`, or another auditor's file.
  There is no importer, no `connector.yaml` `params_model` string, no `__all__`, no
  `__subclasses__()` walk, no discriminated union, no MCP registration.
- **Consequence**: ~46 lines of public pydantic model, plus their docstrings, in the module whose
  own docstring says its one responsibility is *"the shape of a calculation's input, its answer,
  and the geometry both are about"*. The input half of that sentence has had no consumer since
  the physics moved: MCP tools take plain arguments (`compute_xtb_energy(smiles: str, charge: int
  = 0)`), the Temporal wire carries the `*Result` models, and `tests/test_calc_payload_schemas.py`
  pins only the nine result models — the five input models are not in `PAYLOAD_MODELS`, so nothing
  even guards their shape. They are the largest single block of dead public surface in the slice,
  and they are *plausible*: a reader adding a tool will reasonably assume `PkaInput` is the
  contract and wire against it.
- **Evidence**:

  ```
  $ grep -rn "\bXtbInput\b" --include=*.py --include=*.yaml --include=*.json --include=*.toml .
  ./src/chemclaw/science/calc/models.py:160:class XtbInput(BaseModel):
  ./docs/archive/plans/backlog-plan.md:83:...
  ./docs/archive/plans/xtb-tools-proposal.md:42:...
  ```

  Same for `PkaInput`, `SolubilityInput`, `DescriptorInput`, `LogdInput` (only hits outside
  `models.py` are `docs/archive/` and `tasks/audit-*/`). And the pinned set, from
  `tests/test_calc_payload_schemas.py:47-56` and `RECORDED_SHAPES`, contains nine names, none of
  them an `*Input`.

  `XtbInput`'s docstring is itself evidence that it is describing something that is not here any
  more: *"`charge` is redundant with the SMILES — **the server validates it** against the formal
  charge the structure already carries"*. The server that validates it is `Chemclaw3-mcp`, and it
  validates the tool's `charge: int` parameter, not this model.
- **Fix**: Delete all five classes. Behaviour-preserving — nothing constructs, validates against,
  or generates a schema from any of them. If the intent is to publish a request contract for
  `Chemclaw3-mcp`, that contract belongs in the repo that serves it, not as an unimported mirror
  here.

---

## `uncertainty.structural_domain` is dead code kept alive by its own test

- **Severity**: high
- **Location**: `src/chemclaw/science/calc/uncertainty.py:161` (`structural_domain`), `:61`
  (`_ORGANIC_ELEMENTS`), and `:34-43` of the module docstring
- **Trigger**: `grep -rn "structural_domain" src/` returns exactly one hit — the `def` itself.
  The only other references in the tree are `tests/test_uncertainty.py:26,47,59,70,77`.
  `_ORGANIC_ELEMENTS` has no reader but `structural_domain`.
- **Consequence**: 36 lines of code and roughly 25 lines of module prose exist to argue for and
  implement an applicability-domain check that **nothing in this process runs**. The check's only
  historical caller was the ESOL solubility predictor, which is now a `Chemclaw3-mcp` server;
  `SolubilityResult.estimate` arrives over the wire already populated, so the domain decision is
  made in the other repository by the other repository's copy. This is precisely the shape
  `CLAUDE.md` names as a failure (`reject_widening`: "a guard with no caller, kept alive by a test
  that calls it directly, is … a claim that a control exists"). The claim here is stronger than
  usual because the docstring is written in the present tense about behaviour that has moved:
  *"`predict_solubility` on an organometallic returned a confident value with a training-set RMSE
  attached"* — `predict_solubility` is not in this repository, and whether its domain check runs
  is not decidable from this file.
- **Evidence**:

  ```
  $ grep -rn "structural_domain\|_ORGANIC_ELEMENTS" --include=*.py src/
  src/chemclaw/science/calc/uncertainty.py:61:_ORGANIC_ELEMENTS = frozenset({...})
  src/chemclaw/science/calc/uncertainty.py:161:def structural_domain(mol: Chem.Mol) -> tuple[bool, tuple[str, ...]]:
  src/chemclaw/science/calc/uncertainty.py:190:    foreign = sorted({a.GetSymbol() for a in mol.GetAtoms()} - _ORGANIC_ELEMENTS)
  ```

  For contrast, the *live* half of the same module is reachable:
  `Estimate` is constructed in `src/chemclaw/connectors/qm/knowledge.py:44`, and
  `CalculationDomainError` is raised from `logd.py` and listed in
  `src/chemclaw/durable/publish.py:58`.
- **Fix**: Delete `structural_domain`, `_ORGANIC_ELEMENTS`, the paragraph of the module docstring
  that argues for them ("**On what a domain check may honestly assert here**"), and
  `tests/test_uncertainty.py`'s four tests of it. Behaviour-preserving in this repository. Keep
  `Estimate`, `Method`, `_METHOD_PROSE` and `CalculationDomainError`, which all have production
  callers. If the mcp server does *not* have its own copy, that is a correctness gap in the mcp
  repo and should be fixed there — the wrong repair is to leave an uncalled function here so the
  logic "exists somewhere".

---

## `default_store` is defined twice, and the surviving docstring claims it is defined once

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/postgres_store.py:168` (`default_store`) — clone site
  two is `src/chemclaw/connectors/calc/server/tools.py:70`
- **Trigger**: Import both and compare.
- **Consequence**: Two functions with byte-identical bodies returning the same object. The one in
  my slice carries this docstring:

  > *"The one place that names the production backend, so a tool module does not have to know
  > which one it is."*

  A tool module — `connectors/calc/server/tools.py`, the largest consumer of this store — does in
  fact know which one it is: it imports `PostgresStore` directly and constructs it. The stated
  reason for the duplication cannot be the test seam, because the seam works without it: every
  *other* importer (`connectors/bo/server/tools.py`, `connectors/bo/activities.py`,
  `connectors/calc/activities.py`, `connectors/qm/activities.py`) does
  `from chemclaw.science.calc.postgres_store import default_store` and its tests monkeypatch the
  attribute on the importing module (`tests/test_bo_tools.py:197`,
  `tests/test_calc_jobs.py:66`, `tests/test_qm_workflow.py:304`). The cost is the ordinary cost of
  a clone: a change to how the production store is constructed (a DSN, a pool, a wrapper) applied
  in `postgres_store.py` silently misses the calc tool server.
- **Evidence**:

  ```
  $ uv run python -c "..."
  A body: return PostgresStore()
  B body: return PostgresStore()
  same class: True PostgresStore
  same dsn: True
  ```
- **Fix**: Delete `connectors/calc/server/tools.py:70-72` and add `default_store` to the existing
  `from chemclaw.science.calc.postgres_store import PostgresStore` line (dropping `PostgresStore`
  if it then has no other use). `tests/test_calc_tools.py` and `tests/test_calc_find.py` already
  patch `tools.default_store`, which continues to work on an imported name. Behaviour-preserving.
  Also correct the `postgres_store.default_store` docstring's stale "Rule of Three: two callers"
  count — there are four importers.

---

## `CALCULATION_EPOCH` is folded into a key at two sites, and `CalculationKey.build` says it is one

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/store.py:102-120` (`CalculationKey.build`) — second
  fold site is `src/chemclaw/connectors/calc/remote.py:254-260` (`remote_key`)
- **Trigger**: Read `build`'s docstring against the module comment 40 lines above it and against
  the only other `CalculationKey(...)` construction that mints a *new* key.
- **Consequence**: `build`'s docstring asserts

  > *"The single place a key is assembled, which is why `CALCULATION_EPOCH` is folded in here: no
  > calculator can be keyed without it, and none has to remember to ask."*

  Both clauses are false. `store.py:34-38`, in the same file, records the opposite and records the
  measured consequence: *"For one release after the physics moved it was folded in* **neither**
  *place for `calc`, so a bump invalidated DFT rows and nothing else while this comment,
  `science/calc/__init__.py` and a test's own failure message all prescribed bumping it as the
  remedy."* So a calculator *was* keyed without it, and somebody *did* have to remember to ask.
  The current state is that the epoch's meaning — "our side changed, invalidate everything" — is
  implemented twice, in two packages, with no shared code and no test that would notice a third
  key-minting site appearing without it. The failure mode is silent by construction: a missing
  epoch fold produces keys that are perfectly valid and simply never invalidate.
- **Evidence**: `grep -rn "CalculationKey(" --include=*.py src/` returns exactly three sites —
  the class definition, `postgres_store.py:154` (reconstructing a key from its own DB columns,
  not minting one), and `connectors/calc/remote.py:254`. The latter's own comment concedes the
  split:

  ```python
  # **The epoch is folded in on this side, because nothing else does it any more.**
  ...
  params_hash=stable_hash({"epoch": CALCULATION_EPOCH, "remote_params": key["params_hash"]}),
  ```

  Compare `build`, which computes `stable_hash({"epoch": CALCULATION_EPOCH, "params": params})`.
  Two hand-written spellings of one rule, differing only in the inner key name.
- **Fix**: Add a second constructor beside `build` in `store.py`, so `CalculationKey` is again the
  only type that knows the epoch exists:

  ```python
  @classmethod
  def from_server(cls, calc_type: str, calc_version: str, input_hash: str,
                  params_hash: str) -> "CalculationKey":
      """Rebuild a key the calculation server derived, folding our epoch into its params hash."""
      return cls(calc_type=calc_type, calc_version=calc_version, input_hash=input_hash,
                 params_hash=stable_hash({"epoch": CALCULATION_EPOCH,
                                          "remote_params": params_hash}))
  ```

  `remote_key` then calls it, keeping the exact same hash payload — **behaviour-preserving**, and
  `tests/test_calc_remote.py:160` (which pins the literal
  `{"epoch": CALCULATION_EPOCH, "remote_params": "a075a6029c28d314"}` digest) will confirm that.
  Rewrite `build`'s docstring to say what is true: it is the local-key constructor, `from_server`
  is the remote one, and *both* fold the epoch. A `tests/test_upstream_surface.py`-style assertion
  that no module outside `store.py` calls `CalculationKey(` with a computed `params_hash` would
  make the invariant checkable rather than believed.

---

## The `find_calculations` refusal advertises a `calc_type` that no row can carry

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/store.py:157` (`STRUCTURE_KEYED_PREFIXES`) and
  `:197-208` (`_molecule_filter_addresses_the_type`)
- **Trigger**: An agent calls `find_calculations(smiles="CCO", calc_type="xtb.opt")`. The
  validator refuses and tells it to *"ask for a molecule-keyed calculation (pka, solubility,
  **descriptors**, dft)"*. The agent follows the advice and calls
  `find_calculations(smiles="CCO", calc_type="descriptors")`.
- **Consequence**: The second call is accepted, runs
  `WHERE calc_type = 'descriptors'`, and returns `[]` — for every molecule, forever. The
  descriptor panel is stored under the calc type `developability`, not `descriptors`
  (`tests/calc_server_fake.py:56`, mirroring the real server). The tool's own contract says *"An
  empty result is a real answer — say the store has nothing"*, so the model reports "we have never
  computed descriptors for this molecule" as a fact. That is exactly the misreading the validator
  was written to prevent — its docstring says the refusal exists *"because the empty list would
  read as 'nothing has been computed' when the truth is 'that family cannot be looked up this
  way'"* — reintroduced one hop later by the refusal's own remedy.

  The same message is repeated verbatim in the agent-facing tool docstring
  (`connectors/calc/server/tools.py:227`), which additionally gives `"xtb"` as an example
  `calc_type` — also unmatchable, since the stored types are `xtb.sp`, `xtb.opt`, `xtb.hess`, …
  and `find` compares with `=`, not `LIKE`.

  Separately, `STRUCTURE_KEYED_PREFIXES`'s second entry, `"geometry."`, guards a family that no
  longer exists: the two geometry builders are explicitly *unkeyed* now
  (`tests/calc_server_fake.py:66`: `_UNKEYED = frozenset({"predict_logd", "embed_structure",
  "combine_structures"})` — *"the two geometry builders are not compute tools, so the real server
  refuses them by name"*). No row can have a `geometry.` type, so half the constant is dead
  configuration.
- **Evidence**:

  ```
  $ uv run python -c "..."
  prefixes: ('xtb.', 'geometry.')
  REFUSAL MESSAGE: Value error, 'xtb.opt' is keyed by 3-D structure, not by molecule, so it
    cannot be found by SMILES. Query it by type alone, or ask for a molecule-keyed calculation
    (pka, solubility, descriptors, dft).
  accepted calc_type=descriptors -> descriptors
  ```

  Molecule-keyed types that actually exist: `pka`, `solubility`, `developability`
  (`tests/calc_server_fake.py:54-56`) and `dft` (`connectors/qm/cache.py:CALC_TYPE`).
- **Fix**: Two changes, both behaviour-preserving except that the message becomes true.
  (a) Replace `descriptors` with `developability` in the message at `store.py:206` and in the
  `find_calculations` docstring, and replace the `"xtb"` example with `"xtb.opt"`.
  (b) Drop `"geometry."` from `STRUCTURE_KEYED_PREFIXES`, leaving `("xtb.",)`. Better still, stop
  hand-maintaining the list of names in prose: the honest remedy for a refused query is *"query it
  by `calc_type` alone with no `smiles`"*, which is true of every structure-keyed family without
  naming any of them, and cannot go stale when the server renames a type.

---

## `_MEDIA_TYPES` is a ten-entry table of which two entries are reachable

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/artifacts.py:235` (`_MEDIA_TYPES`), `:255`
  (`media_type_for`), `:201` (`put_all`)
- **Trigger**: Trace every writer into the artifact store. `media_type_for` has exactly one
  production caller (`put_all`, `artifacts.py:220`); `put_all` has exactly one production caller
  (`ArrayOffloadingStore.put`, `artifacts.py:344`); that call passes `files` whose keys come from
  the `fields` mapping, and the only mapping ever supplied is `HESSIAN_ARRAYS`
  (`connectors/calc/compose.py:219`), which has two entries. No other module in `src/` calls
  `ArtifactStore.put` at all.
- **Consequence**: Eight of the ten media types are unreachable, and two of those the table itself
  admits are speculative (*"Reserved for the DFT tier … nothing writes these yet"*). The other six
  — `hessian`, `vibspectrum`, `xtbopt.xyz`, `crest_conformers.xyz`, `crest_rotamers.xyz`,
  `cre_members` — are the xtb/CREST driver's captured files, and that driver is in `Chemclaw3-mcp`
  now. So the whole three-link chain `ArrayOffloadingStore.put → put_all → media_type_for →
  _MEDIA_TYPES` resolves, in every reachable path, to two constants that `HESSIAN_ARRAYS` already
  names. `put_all`'s docstring still describes the world where this paid for itself: *"The single
  call site **the calculators** share (DRY): capture hands back a `{name: bytes}` map"* — there
  are no calculators here and nothing captures.

  The downstream consequence is worth stating even though it lands outside this slice: because
  every artifact this repo writes is a packed `.npy`, and `fetch_artifact`
  (`connectors/calc/server/tools.py:343`) refuses binary artifacts by design, `fetch_artifact` can
  never successfully read anything this repository stores.
- **Evidence**: instrumented `media_type_for` and ran the production-path suites
  (`tests/test_calc_compose.py`, `tests/test_calc_tools.py`, `tests/test_calc_thermo.py` — the
  three that drive `compose`/`tools` end to end; `tests/test_artifacts.py` excluded because it
  calls `put_all` and `media_type_for` with hand-written names directly):

  ```
  $ uv run python /tmp/probe_media.py
  47 passed, 3 warnings in 1.67s
  NAMES REACHING media_type_for: ['dipole_derivatives.npy', 'hessian.npy']
  ```

  With `tests/test_artifacts.py` included, the extra names are exactly the three the test itself
  types out: `['dipole_derivatives.npy', 'hessian', 'hessian.npy', 'something.unknown',
  'xtbopt.xyz']`.
- **Fix**: Collapse the three-link chain into one table. `HESSIAN_ARRAYS` already maps payload
  field → artifact name; make it map payload field → `(artifact name, media type)` and have
  `ArrayOffloadingStore.put` read the type from it, then delete `_MEDIA_TYPES`, `media_type_for`
  and `put_all` (its loop is six lines inlined at its single caller). Behaviour-preserving: the
  two live names keep their current types (`application/x-npy`). Keep the `media_type` column and
  the `ArtifactRef.media_type` field — rows written by other producers, including a future DFT
  tier, still carry theirs. The four names that a real producer would need again (`hessian`,
  `xtbopt.xyz`, …) belong with that producer, which is the mcp server.

  A cheaper variant, if the table is wanted as documentation of a cross-repo naming convention:
  keep `_MEDIA_TYPES` but delete the two reserved-for-DFT rows and note in the docstring that the
  file names are the mcp server's, not this repo's. That does not remove the indirection, only the
  fiction.

---

## `tblite` is a declared runtime dependency with no importer under `src/`

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/solvents.py:44` (`ALPB_SOLVENTS`) — the constant this
  dependency exists to validate; `pyproject.toml:152` is the declaration
- **Trigger**: `grep -rn "tblite" --include=*.py src/` — every hit is a docstring or a comment.
  The only real `import tblite` in the repository is `tests/test_solvents.py:39,78`.
- **Consequence**: `deploy/Containerfile:101` runs `uv sync --frozen --no-dev`, which installs the
  main dependency group, so ~17.5 MB of compiled quantum-chemistry library and its bundled Fortran
  shared objects ship in every OpenShift image and every worker pod to support zero production
  imports. `solvents.py`'s own docstring explains the constraint that made this module leaf-light
  in the first place — *"it must not drag `tblite` in with it (D-118,
  `tests/test_connector_isolation.py`)"* — which the module honours and the dependency list
  undoes at the package level.

  There is a second-order design point in the same place: the docstring claims
  *"`tests/test_solvents.py` re-derives it against the installed tblite, so an upgrade that adds
  or drops a solvent fails a test instead of surfacing as a wrong refusal."* That is still
  literally true, but the tblite the refusal now has to agree with is the one **inside
  `Chemclaw3-mcp`'s image**, not the one in this repo's lockfile. The test pins this repo's copy;
  the calculation runs against the other. A version skew between the two produces exactly the
  failure the module was built to end — a solvent that passes the precondition and dies in the
  activity — with the test green.
- **Evidence**:

  ```
  $ grep -rn "tblite" --include=*.py src/ | grep -v '"""' | grep -c "^import\|^from"
  0
  $ du -sh .venv/lib/python3.11/site-packages/tblite*
  7.6M    .venv/lib/python3.11/site-packages/tblite
  9.9M    .venv/lib/python3.11/site-packages/tblite.libs
  $ sed -n 101p deploy/Containerfile
  RUN uv sync --frozen --no-dev \
  ```

  The declaration's own justification comment (`pyproject.toml:146-151`) names two modules that no
  longer exist — *"Promoted from transitive … when `calc.xtb_opt` and `calc.xtb_thermo` began
  importing it directly: the L-BFGS-B optimizer over tblite's analytic gradient"*. (`scipy`, the
  line that comment is attached to, **is** still imported — `thermo.py:41`,
  `from scipy.linalg import null_space` — so scipy stays and only the rationale is stale.)
- **Fix**: Move `tblite` from `[project].dependencies` to the `dev` dependency group so the image
  loses it and `tests/test_solvents.py` keeps working under `uv sync`. Behaviour-preserving for
  every runtime path, since nothing under `src/` imports it. Then rewrite the `solvents.py`
  paragraph to say what the test actually proves — that the constant agrees with *a* tblite of
  the pinned version — and record the real re-derivation check as one that has to run against the
  server's tblite. The stronger version of the same fix is to stop asking this repo about ALPB at
  all: the calculation server knows its own solvent table, and a `supported_solvents` query at
  precondition time cannot skew.

---

## `is_supported` has no production caller

- **Severity**: low
- **Location**: `src/chemclaw/science/calc/solvents.py:130`
- **Trigger**: `grep -rn "is_supported" src/` → one hit, the `def`. Only
  `tests/test_solvents.py:117-119` calls it.
- **Consequence**: A one-line public wrapper over `_normalize(name) in ALPB_SOLVENTS`. The
  production path (`require_supported_solvents` → `unsupported`) reimplements the same membership
  test inline at `:147` (`if key in seen or key in ALPB_SOLVENTS`) rather than calling it, so the
  function is both dead and duplicated. Small, but it is the third distinct "public symbol whose
  only caller is its test" in this slice, which is the pattern rather than the instance.
- **Evidence**:

  ```
  $ grep -rn "\bis_supported\b" --include=*.py src/ tests/
  src/chemclaw/science/calc/solvents.py:130:def is_supported(name: str) -> bool:
  tests/test_solvents.py:18:    is_supported,
  tests/test_solvents.py:117:    assert is_supported("THF")
  tests/test_solvents.py:118:    assert is_supported(" Water ")
  tests/test_solvents.py:119:    assert not is_supported("2-MeTHF")
  ```
- **Fix**: Either delete it and drop the three assertions (the same three cases are covered by
  `unsupported`, which the same test file already exercises), or use it inside `unsupported` so
  the membership rule has one spelling. Behaviour-preserving either way.

---

## What I checked and found sound

Stated so the absence of a finding is a result rather than a gap.

- **No layering violation.** Every `chemclaw` import in the slice is `chemclaw.core.*` or a
  sibling in `science.calc`; nothing reaches into `connectors`, `durable`, `agent`, `api`, `kg` or
  `retrieval`. Verified by grepping all eleven modules' import lines.
- **The one deferred import earns its keep.** `models.py:105` imports `core.config` inside
  `Structure._normalize_and_validate` with the comment *"this module is imported by
  `connectors/calc/results.py` on a path that must stay leaf-light"*. I checked the claim rather
  than taking it: `import chemclaw.connectors.calc.results` leaves `chemclaw.core.config` out of
  `sys.modules`. It is true, and moving the import to module scope would break it.
- **No module-global mutable state.** `InMemoryStore` and `InMemoryArtifactStore` hold their
  dictionaries on the instance; the module-level names are all frozen constants
  (`ALPB_SOLVENTS` is a `frozenset`, `HESSIAN_ARRAYS` a `MappingProxyType`, `_MEDIA_TYPES` and
  `_MODE_FIELD` plain dicts that nothing mutates).
- **`_matches` is genuinely shared, not cloned.** `store.py:271` is the single predicate the
  in-memory backend uses, and `postgres_store.py`'s `_FIND` expresses the same filters as SQL
  because it must filter before fetching — one rule, two necessary encodings, with
  `tests/test_postgres_store.py:147` pinning the agreement. Not a duplication finding.
- **`_inertia` is correctly shared** between `_rotational` and `_vibrational_basis`
  (`thermo.py:141`), which is what stops "is this molecule linear" being answered two ways. It is
  recomputed twice per `thermochemistry_from_hessian` call, which is an eigendecomposition of a
  3×3 — not worth a finding.
- **`ArrayOffloadingStore` is the right shape.** Expressing array offloading as a `ResultStore`
  decorator rather than as a second caching path is what keeps `cached_remote` and its callers
  unchanged; it has one real caller today but the alternative is threading a flag through the
  whole compute path. Kept.
- **No hardcoded config.** Every threshold, cap and default in the slice reads from `settings`
  (`xtb_geometry_decimals`, `artifact_max_bytes`, `artifact_compression_level`,
  `calibration_min_observations`, `logd_default_ph`, `logd_negligible_ionised_fraction`,
  `xtb_rrho_cutoff_cm`, `xtb_imaginary_kick_angstrom`, …). The bare numbers that remain are CODATA
  physical constants and unit conversions (`thermo.py:58-83`), which are not configuration.
- **No error type that forces string-matching.** `CalculationDomainError` subclasses
  `ChemclawError`; callers catch the type. `decode` raises a specific `ValueError` on an unknown
  codec rather than returning the payload.
- **`provenance` is honest about being unused.** `StoredResult.provenance` is only ever
  `"computed"` in `src/`, and its docstring says so plainly (*"no code branches on it"*). It is
  surfaced read-only by `find_calculations`. Not a finding — it is a two-word column, correctly
  labelled as audit metadata.
