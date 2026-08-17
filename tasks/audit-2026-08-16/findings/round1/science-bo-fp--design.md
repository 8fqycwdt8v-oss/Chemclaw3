# Round 1 — `science/bo/` + `science/fingerprints/` — design & simplification

Slice: `src/chemclaw/science/bo/` (problem, engine, campaign, campaign_record,
campaign_record_store, objectives, featurize, progress, benchmarks) and
`src/chemclaw/science/fingerprints/` (store, molfp, rxnfp).
Every file listed above was read in full. Scripts run under `/tmp` are quoted verbatim below.

---

## `science/bo/campaign.py` is a whole module with no production caller, and it holds a second copy of the launch preconditions

- **Severity**: medium
- **Location**: `src/chemclaw/science/bo/campaign.py:1-105` (`optimize`, `_evaluate`, `Evaluate`);
  clone sites `src/chemclaw/science/bo/campaign.py:81-94` vs
  `src/chemclaw/science/bo/problem.py:857-868`
- **Trigger**: Any change to the BO launch rules — e.g. adding a fourth `require_*` rule to
  `require_campaign_startable`, or changing what "this problem is multi-objective" refuses.
- **Consequence**: Two things at once.

  (a) `optimize` has zero non-test references anywhere in `src/`, `examples/`, `scripts/` or
  `infra/`. The durable path (`connectors/bo/workflows.py:136-157`) re-implements the ask/tell
  loop itself over Temporal activities and imports only `best_of`, `space_exhausted` and
  `discrete_candidate_count` from `problem`. So the module's own docstring — *"`evaluate` is
  injected — an analytic function in tests, a Phase-1c calculator (through the store) **in real
  use**"* — describes a caller that does not exist. 105 lines of source plus ~950 lines across
  three test files (`tests/test_bo.py`, `tests/test_bo_campaign.py`, `tests/test_reizman.py`)
  exercise a loop nothing runs, while the loop that *is* run lives in `workflows.py`.

  (b) Because it is a second entry point it carries its own copy of the precondition block. The
  same four rules appear twice with different wording, so the two can drift silently — and the
  multi-objective refusal now exists in **three** near-identical spellings.

- **Evidence**: reference sweep (`/tmp/deadsweep.py`, run over `src/ examples/ scripts/ infra/`,
  excluding the defining file and comment/docstring lines):

  ```
  --- optimize: 0 reference(s) outside src/chemclaw/science/bo/campaign.py
  ```

  and every import of the module:

  ```
  ./tests/test_bo_campaign.py:27:from chemclaw.science.bo.campaign import optimize
  ./tests/test_bo.py:20:from chemclaw.science.bo.campaign import optimize
  ./tests/test_reizman.py:18:from chemclaw.science.bo.campaign import optimize
  ```

  The clone. `campaign.py:81-94`:

  ```python
  require_rounds_within_ceiling(n_rounds)
  require_names_do_not_clash(problem)
  require_descriptors_distinguish_categories(problem)
  if len(problem.objectives) > 1:
      named = ", ".join(objective.name for objective in problem.objectives)
      raise ValueError(
          f"this loop returns one best observation and this problem has "
          f"{len(problem.objectives)} objectives ({named}), which have no single best point. ...
  ```

  `problem.py:857-868` (`require_campaign_startable`):

  ```python
  require_rounds_within_ceiling(spec.n_rounds)
  require_names_do_not_clash(spec.problem)
  require_descriptors_distinguish_categories(spec.problem)
  require_direction_matches_objective(spec)
  if len(spec.problem.objectives) > 1:
      named = ", ".join(objective.name for objective in spec.problem.objectives)
      raise ValueError(
          f"the durable campaign evaluates one registered objective per round, ...
  ```

  and a third at `problem.py:970-975` (`best_of`): same `len(...) > 1` / `", ".join(...name...)`
  / raise-with-alternative shape, third wording.

- **Fix**: Delete `src/chemclaw/science/bo/campaign.py`. Behaviour-preserving for production (no
  caller). The three test files that drive `optimize` get a five-line local ask/tell helper in
  `tests/` — that is honest about what they are, an engine integration harness, and it removes the
  pressure to keep a second precondition block in sync. If the module is instead kept, make
  `optimize` call `require_campaign_startable`'s rules rather than re-listing three of them, and
  extract the multi-objective refusal to one
  `require_single_objective(problem, alternative: str) -> None` used by all three sites
  (behaviour-preserving apart from the message text, which is currently three different sentences
  for one condition).

---

## `store.tanimoto` is dead, and the width guard it owns is copy-pasted into all three live call sites

- **Severity**: medium
- **Location**: `src/chemclaw/science/fingerprints/store.py:35-47` (`tanimoto`); duplicated guard at
  `store.py:289-290` (`InMemoryFingerprintStore.find_similar`) and
  `src/chemclaw/memory/similarity.py:52-56` (`cluster_by_similarity`)
- **Trigger**: Any similarity search or clustering pass. Also: changing the guard — e.g. making the
  message name the two widths — now means editing three places, one of which nothing calls.
- **Consequence**: The function that is supposed to be the one definition of "compare two
  fingerprint bitstrings safely" is never invoked, and the invariant it encodes (`FingerprintError`
  on unequal widths) is re-written by hand at each of the two places that actually compare. Its own
  docstring asserts a caller that does not exist — *"Works on the stored bitstrings directly, so the
  in-memory backend ranks without the source cheminformatics library"* — but
  `InMemoryFingerprintStore.find_similar` (line 295) calls `tanimoto_bits`, not `tanimoto`. So does
  `tanimoto_bits`' own docstring: *"`tanimoto` stays the two-string form for everyone else"* — there
  is no everyone else.
- **Evidence**: `/tmp/tanimoto_probe.py` monkeypatches `store.tanimoto` with a counting spy and runs
  both production search entry points against an in-memory index:

  ```
  molecule hits: [('CCO', 1.0), ('CCCO', 0.556), ('CC(=O)O', 0.182)]
  reaction hits: [('rx1', 1.0)]
  store.tanimoto call count during both searches: 0
  occurrences of the width-guard string in store.py: 2
  occurrences in memory/similarity.py: 1
  ```

  and the reference sweep:

  ```
  --- tanimoto: 0 reference(s) outside src/chemclaw/science/fingerprints/store.py
  ```

  (`tanimoto` appears only in `tests/test_molfp.py:65-67` and `tests/test_rxnfp.py:159-162`.)

- **Fix**: Delete `tanimoto`, rename `tanimoto_bits` to `tanimoto`, and give the width check one
  home as `require_equal_width(bits_a: str, bits_b: str) -> None` (or, better, a
  `parse_fingerprint(bits, width) -> int`) that `InMemoryFingerprintStore.find_similar` and
  `cluster_by_similarity` both call. Behaviour-preserving: the two live sites already raise the same
  `FingerprintError` with the same message; the only change is that the string exists once. Update
  the two tests to the int form (they compare fixed-width bitstrings they build themselves).

---

## `campaign_record.py`'s "dependency-free half" claim is false — it imports psycopg at module level

- **Severity**: medium
- **Location**: `src/chemclaw/science/bo/campaign_record.py:26-35` (`import psycopg`) and
  `:348` (`_TRANSIENT_WRITE_FAILURES`); claim in the module docstring at `:22-27`
- **Trigger**: `import chemclaw.science.bo.campaign_record` in any process — the agent, a connector
  worker, a CLI run — regardless of `session_store`.
- **Consequence**: The whole reason this module is split from `campaign_record_store.py` is stated
  as *"so a process without Postgres never pulls a driver for a store it will not use"*, citing
  `chemclaw.kg.proposal` as the pattern. The driver is pulled anyway, by this module, at line 35.
  The lazy import at `:340` therefore defers only the ~190-line store class, not the dependency —
  the split buys nothing it claims to buy, while costing a two-file structure and a second
  `CampaignStore` implementation to keep in sync.
- **Evidence**:

  ```
  $ uv run python -c "import chemclaw.science.bo.campaign_record; ..."
  psycopg imported by campaign_record: True
  psycopg_binary/_psycopg loaded: ['psycopg.pq._enums', 'psycopg._compat', 'psycopg.pq.abc',
    'psycopg.pq.misc', 'psycopg_binary.version', 'psycopg_binary', 'psycopg.errors',
    'psycopg._encodings']
  campaign_record_store imported: False
  ```

  The module it names as the pattern does *not* do this — `src/chemclaw/kg/proposal.py` imports
  (lines 22-35) contain no psycopg, and its equivalent swallow is a bare
  `except Exception` (`proposal.py:297-300`). The divergence is exactly the narrow tuple at
  `campaign_record.py:348`:

  ```python
  _TRANSIENT_WRITE_FAILURES = (ConnectionError, OSError, TimeoutError, psycopg.Error)
  ```

- **Fix**: Resolve `psycopg.Error` lazily and delete the module-level import — a `psycopg.Error`
  can only be raised in a process that has already imported psycopg (the store did it), so:

  ```python
  def _is_transient_write_failure(error: BaseException) -> bool:
      if isinstance(error, ConnectionError | OSError | TimeoutError):
          return True
      psycopg = sys.modules.get("psycopg")
      return psycopg is not None and isinstance(error, psycopg.Error)
  ```

  Behaviour-preserving (identical set of caught exceptions in every reachable state) and it makes
  the docstring's claim true and testable — a test can assert `"psycopg" not in sys.modules` after
  importing `campaign_record` in a fresh interpreter. Alternatively, if the coupling is judged
  acceptable, delete the claim and merge the two files back into one.

---

## `fingerprints/store.py` is not domain-neutral: it names both domains and is in a proven import cycle with them

- **Severity**: medium
- **Location**: `src/chemclaw/science/fingerprints/store.py:28` (`Subject` literal),
  `:526-541` (`default_molecule_store`, `default_reaction_store`), with function-local imports at
  `:528` and `:538`
- **Trigger**: Adding a third fingerprint domain, or moving either factory's import to module level.
- **Consequence**: The module docstring says *"the record shape, the Tanimoto ranking, the store
  interface, and both backends are domain-neutral … Each domain supplies only its own fingerprint
  function, its table, and its bit width."* In fact `store.py` hardcodes both domains three times
  over — the closed `Subject = Literal["molecule", "reaction"]`, the two factories naming
  `"molecule_fingerprints"`/`"reaction_fingerprints"`, and `settings.ecfp_bits`/`settings.drfp_bits`.
  The dependency therefore runs both ways (`molfp.fingerprint` → `store` for `FingerprintError`;
  `store` → `molfp.fingerprint` for `molecule_definition`), and the *only* two function-local
  imports in the whole package exist to hide that cycle. A third domain requires editing the
  "neutral" module in three places, which is precisely the coupling the split was meant to remove.
- **Evidence**: an import-position sweep over the package (`/tmp/cycle.py`) — the two store
  factories are the only function-local fingerprint imports anywhere:

  ```
  molfp/fingerprint.py:18  module-level: from ...store import FingerprintError
  molfp/search.py:19       module-level: from ...molfp.fingerprint import ecfp_bitstring, ...
  rxnfp/fingerprint.py:14  module-level: from ...store import FingerprintError
  rxnfp/search.py:9        module-level: from ...rxnfp.fingerprint import drfp_bitstring, ...
  store.py:528             FUNCTION-LOCAL: from ...molfp.fingerprint import molecule_definition
  store.py:537             FUNCTION-LOCAL: from ...rxnfp.fingerprint import reaction_definition
  ```

  Hoisting them proves the cycle (patch applied, run, reverted — `git diff --stat` clean):

  ```
  File ".../science/fingerprints/store.py", line 23, in <module>
      from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
  File ".../science/fingerprints/molfp/fingerprint.py", line 18, in <module>
      from chemclaw.science.fingerprints.store import FingerprintError
  ImportError: cannot import name 'FingerprintError' from partially initialized module
    'chemclaw.science.fingerprints.store' (most likely due to a circular import)
  ```

- **Fix**: Break the cycle by moving `FingerprintError` (and, with it, `Subject`) into a leaf
  module — `science/fingerprints/errors.py` — that both `store` and the two `fingerprint` modules
  import downward. Then move `default_molecule_store` into `molfp/` and `default_reaction_store`
  into `rxnfp/`, where the table name, bit width and definition already belong, and both imports
  become ordinary module-level ones. Behaviour-preserving: every caller of the two factories
  (`durable/eln_sync.py`, `agent/research_tools.py`, `durable/report_workflow.py`, the two
  connector `tools.py`) already imports from a specific domain's world, so only the import line
  changes. `Subject` widens to whatever the third domain needs without touching `store.py`.

---

## Three engine helpers whose stated reason is a caller that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/science/bo/engine.py:498-525` (`predict_at`), `:569-592`
  (`surrogate_fit_quality`), `:172-179` (`_objective_output`)
- **Trigger**: Reading `interrogate_surrogate`'s docstring (`engine.py:662-664`) to find out why the
  two wrappers exist.
- **Consequence**: `interrogate_surrogate` says *"`predict_at` and `surrogate_fit_quality` are thin
  wrappers over this, so no caller can accidentally take the two halves from two fits."* Neither
  wrapper has a non-test caller — the one production consumer,
  `connectors/bo/server/tools.py:50`, imports `interrogate_surrogate` directly. The guard is
  guarding nobody, and two public names in the BoFire boundary module exist only to be tested.
  `_objective_output`'s docstring says it is *"Kept for the classical design **paths**"* (plural);
  it has exactly one call site (`:800`, inside `_fractional_design`) — `_full_design` goes through
  `_to_domain`/`_outputs` instead.
- **Evidence**: reference sweep over `src/ examples/ scripts/ infra/`:

  ```
  --- predict_at: 0 reference(s) outside src/chemclaw/science/bo/engine.py
  --- surrogate_fit_quality: 0 reference(s) outside src/chemclaw/science/bo/engine.py
  --- _objective_output: 0 reference(s) outside src/chemclaw/science/bo/engine.py
  ```

  and inside `engine.py`:

  ```
  172:def _objective_output(problem: OptimizationProblem) -> ContinuousOutput:
  800:        outputs=Outputs(features=[_objective_output(problem)]),
  ```

  No dynamic registration reaches them: `connectors/bo/connector.yaml` lists tool names only
  (`suggest_next_experiment`, `resume_campaign`, `generate_screening_design`, `campaign_progress`,
  `predict_outcome`), and there is no `getattr`/`import_module`/`entry_points` anywhere under
  `connectors/bo/` or `science/bo/`.

- **Fix**: Delete `predict_at` and `surrogate_fit_quality`; re-point `tests/test_bo_predict.py` at
  `interrogate_surrogate` (which it already calls at line 398 to prove the two agree). Inline
  `_objective_output` into `_fractional_design` as `_outputs(problem)[0]` and drop the misleading
  plural. All three are behaviour-preserving — the wrappers add nothing but argument shuffling.

---

## `Objective` means a pydantic spec in one module and an async callable in the next, and the callable type is spelled three times

- **Severity**: low
- **Location**: `src/chemclaw/science/bo/problem.py:128` (`class Objective(BaseModel)`) vs
  `src/chemclaw/science/bo/objectives.py:27` (`Objective = Callable[...]`); the rename at
  `objectives.py:23-25`; the third spelling at
  `src/chemclaw/science/bo/benchmarks/reizman_suzuki.py:98-100`; the fourth alias at
  `src/chemclaw/science/bo/campaign.py:29`
- **Trigger**: Reading or editing `objectives.py`, which must import its neighbour's `Objective`
  under an assumed name to say anything about the direction field.
- **Consequence**: In one module `Objective` is *what to optimize and which way*; in the sibling it
  is *the function that evaluates it*. `objectives.py` collides with itself and resolves it with
  `from chemclaw.science.bo.problem import Objective as ObjectiveSpec`, so the file uses two names
  for the model and one overloaded name for the callable — and `RegisteredObjective.direction`'s
  comment then has to explain that `direction` and `ObjectiveSpec.direction` are "compared as
  equals", a sentence only needed because the types were named as if they were the same thing. The
  callable type itself is written out three times: `objectives.Objective`, `campaign.Evaluate`, and
  inline in `reizman_suzuki.load_benchmark`'s return annotation.
- **Evidence**:

  ```
  problem.py:128            class Objective(BaseModel):
  objectives.py:27          Objective = Callable[[dict[str, ParamValue]], Awaitable[float]]
  objectives.py:23-25       from chemclaw.science.bo.problem import (
                                Objective as ObjectiveSpec,
                            )
  campaign.py:29            Evaluate = Callable[[dict[str, ParamValue]], Awaitable[float]]
  reizman_suzuki.py:98-100  def load_benchmark() -> tuple[
                                OptimizationProblem, Callable[[dict[str, ParamValue]], Awaitable[float]]
                            ]:
  ```

- **Fix**: Rename `objectives.Objective` to `ObjectiveFn`, drop the `ObjectiveSpec` alias (import
  `Objective` plainly), and have `reizman_suzuki.load_benchmark` annotate its second return element
  as `ObjectiveFn`. `campaign.Evaluate` disappears with `campaign.py` (finding 1); if the module is
  kept, alias it to `ObjectiveFn`. Purely behaviour-preserving — a rename plus three annotation
  edits, with no runtime effect.

---

## Checked and found sound (no finding)

Recorded so the absence is informative rather than an omission:

- **`_REGISTRY` in `objectives.py`** is a genuine dispatch table with two real entries and two
  readers (`get_objective`, `registered_direction`) — not a one-caller abstraction. The two do
  duplicate the three-line lookup-and-raise (`objectives.py:130-132` and `:145-147`, identical
  message string); at three lines this is under the threshold I would report, but it is the obvious
  `_registered(name) -> RegisteredObjective` extraction if the file is touched.
- **`_SEED_DRAW_ROUNDS = 8`, `MIN_SEED_OBSERVATIONS = 2`, `_RF_N_ESTIMATORS`/`_RF_RANDOM_STATE`** are
  the only hardcoded numbers in the slice, and each has a stated reason why it is not config
  (respectively: a wedge-guard, a library floor, benchmark reproducibility). No hardcoded URL, path,
  timeout or model name.
- **`InMemoryCampaignStore` and `InMemoryFingerprintStore` are live, not test doubles.**
  `InMemoryCampaignStore` is reached through `campaign_store()` whenever `settings.session_store !=
  "postgres"`; `InMemoryFingerprintStore` is used by `examples/research_demo.py`. Neither is dead.
- **`_resolution`'s `zip(..., strict=False)`** (`engine.py:701`) silently truncates past 26 factors.
  I could not construct a reachable case: `get_generator` refuses every `n_generators` large enough
  to make a >26-factor two-level screen a realistic run count (measured — `n_factors=24,
  n_generators=19` → *"Design not possible, as main factors are confounded with each other"*), and
  the feasible 27/30-factor generators imply 2²²–2²⁴ runs. Not reported.
- **`_SURROGATE_FAILURES` / `SurrogateFitError`** do not force string-matching on callers:
  `durable/publish.py:_BAD_DATA_TYPES` lists it by class *name*, but `tests/test_publish.py:86-87`
  asserts every `ChemclawError` subclass is registered, so a new engine error class fails the suite
  rather than silently becoming retryable.
- **No module-global mutable state** in the slice. The three caches (`campaign_store`,
  `_reizman_suzuki`, `molfp._generator`) are all `functools`-memoized pure factories.
- **No lower-layer-imports-higher violation.** Both `featurize.PropertiesFor` and
  `objectives.LogSFor` are injected callables precisely so `science/` does not import
  `connectors/`; the bindings live in `connectors/bo/calculators.py`. This is correct and matches
  what the docstrings claim.
