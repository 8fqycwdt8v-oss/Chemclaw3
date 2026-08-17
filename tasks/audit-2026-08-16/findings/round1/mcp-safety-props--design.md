# Round 1 — `servers/safety/`, `servers/props/` — design and simplification

Repo: `/workspace/chemclaw3-mcp`. Lens: structure that costs more than it buys — duplication,
single-caller abstractions, dead code, module-global state, error types, misleading names.
All reproductions were run with `uv run` inside the workspace; scripts are under `/tmp/audit/`.

Overall: the two servers are small and the layering (`engine/` -> `tools.py` -> `app.py`) is clean
and actually enforced. Nothing here is an architectural failure. What I found is one real clone
between the two safety screens that has already drifted, one documented invariant the code does not
hold, and a set of smaller inlines/dead surfaces.

---

## The two screens are a verbatim clone, and it has already drifted

- **Severity**: medium
- **Location**:
  - `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:265-281` (`_load_rules`),
    `:422-426` (pair cross-product), `:440` (canonical dedup)
  - `servers/safety/src/chemclaw_mcp_safety/engine/genotox.py:148-161` (`_load_alerts`),
    `:202-206` (pair cross-product), `:220` (canonical dedup)
- **Trigger**: read the two functions side by side; then, in one process, warm the genotox cache and
  repoint `genotox.ALERTS_DIR` at a missing directory.
- **Consequence**: three blocks are byte-identical modulo the model constructed, so every future
  fix to pair matching has to be applied twice — and the parts that *were* allowed to differ have
  already diverged in a way that matters:
  - `_load_rules(directory)` is `lru_cache`d **on its argument**, so `screen.RULES_DIR` can be
    repointed and the corpus-fault path re-enters `read_table`. `_load_alerts()` takes no argument
    and reads the module global inside, so once warmed it can never be re-read.
  - The practical result is that the shared `read_table` failure path — the one that must turn a
    missing/truncated alert table into a refusal rather than a clean screen — is exercised **only**
    through the hazard table. `servers/safety/tests/test_tools.py:307-360` monkeypatches
    `RULES_DIR`; nothing in `servers/safety/tests/` does the equivalent for `ALERTS_DIR`, and
    provably could not.
  - The same asymmetry appears in the "table is empty" guard: `screen.py:274-275` raises
    `SafetyRulesError` explicitly, `genotox.py` relies on `AlertTable.structural`'s
    `Field(min_length=1)` — two mechanisms, two error types, one invariant.
- **Evidence** (`/tmp/audit/e.py`):

  ```
  warm: 1 alert(s)
  after swapping ALERTS_DIR to /nonexistent: 1 alert(s) -- stale cache, no fault raised
  hazard screen raised: SafetyRulesError
  ```

  The duplicated pair block, verbatim in both files:

  ```python
  left = [s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:left"])]
  right = [
      s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:right"])
  ]
  ```

  and the dedup line, identical in both:

  ```python
  canonical = list(dict.fromkeys(str(Chem.MolToSmiles(m)) for m in molecules.values()))
  ```

- **Fix**: `screen.py` is already the shared home (`read_table`, `compile_smarts`,
  `parse_components`, `require_screenable_size` all live there and are imported by `genotox`). Add
  three more to it and use them from both call sites — behaviour-preserving:
  - `compile_pattern_map(structural, pairs)` returning the `{id, id:left, id:right}` map both
    loaders build by hand;
  - `pair_matches(molecules, patterns, pair_id) -> list[tuple[str, str]]` for the cross-product;
  - `canonical_screened(molecules) -> list[str]` for the dedup line.

  Separately, give `_load_alerts` the same `directory` parameter `_load_rules` has (or drop the
  parameter from both and expose a `cache_clear` the tests call) so the two caches invalidate alike,
  and add the genotox mirror of `test_tools.py:307` — a missing alert table must raise, not screen.

---

## `screened` deduplicates spellings, flags do not — contradicting `screen_reaction`'s own docstring

- **Severity**: medium
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:402-441` (`screen_reaction`,
  docstring at `:406-407`), same shape at
  `servers/safety/src/chemclaw_mcp_safety/engine/genotox.py:163-222`
- **Trigger**: pass one substance twice under two spellings, e.g.
  `screen_hazards(["CN=[N+]=[N-]", "[N-]=[N+]=NC"])`, or
  `screen_genotoxic_alerts(["O=[N+]([O-])c1ccccc1", "c1ccccc1[N+](=O)[O-]"])`.
- **Consequence**: the payload contradicts itself. `screened` collapses to one canonical structure
  (its code comment at `:437-439` names exactly this case — "a reaction listing `CCO` and `OCC` is
  one substance written twice"), while `flags` reports the same rule twice and `verdict` reports the
  inflated count. A reader reconciling `flags[].matched` against `screened` finds a `matched` value
  that appears in no screened entry. `screen_reaction`'s docstring states the opposite:
  *"Structural flags from the components are deduplicated per (rule, molecule) so a reagent listed
  twice is reported once."* That holds only for byte-identical strings, because `parse_components`
  keys on the caller's spelling. Pair rules inflate the same way.
- **Evidence** (`/tmp/audit/a.py`):

  ```
  A1 screened: ['CN=[N+]=[N-]']
  A1 flags: [('organic-azide', 'CN=[N+]=[N-]'), ('organic-azide', '[N-]=[N+]=NC')]
  A1 verdict: 2 hazard rule(s) matched (most serious: high). Advisory only — ...

  A3 screened: ['OO', '[BH4-]']
  A3 pair flags: [('oxidizer-with-reductant', 'OO + [BH4-]'),
                  ('oxidizer-with-reductant', '[OH][OH] + [BH4-]')]
  A3 verdict: 4 hazard rule(s) matched (most serious: high). ...

  A4 genotox alerts: [('aromatic-nitro', 'O=[N+]([O-])c1ccccc1'),
                      ('aromatic-nitro', 'c1ccccc1[N+](=O)[O-]')]
  A4 verdict: 2 structural alert(s) matched (Nitroaromatic, Nitroaromatic). ...
  ```

  `"2 structural alert(s) matched (Nitroaromatic, Nitroaromatic)"` about one molecule and one alert
  is the whole finding in one line.
- **Fix**: key `parse_components` on the **canonical** SMILES and keep the caller's first spelling
  as the display value: `dict[str, tuple[str, Chem.Mol]]` mapping canonical -> (as-written,
  molecule). `matched` keeps reporting the caller's spelling (the stated intent), `screened` becomes
  `list(molecules)` with no second dedup, and both flag lists and both verdict counts stop
  double-counting. Not behaviour-preserving on the wire — that is the point — and the changed
  outputs are exactly the two above.

---

## `SafetyRulesError` conflates a caller fault with a corpus fault, and the engine's "one exception type" claim is false

- **Severity**: medium
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:82-95` (`SafetyRulesError`),
  raised at `:222`, `:228`, `:231`, `:257`, `:274`, `:308`, `:356`, `:362`;
  claim at `servers/safety/src/chemclaw_mcp_safety/engine/__init__.py:14-15`
- **Trigger**: any consumer that wants to distinguish "the chemist's SMILES was malformed — re-ask"
  from "this server's rule table is unreadable — stop answering and page someone".
- **Consequence**: one type carries five distinct conditions (unparseable SMILES, empty list,
  oversize list, missing corpus, malformed corpus). The only way to tell them apart is to match on
  the message text, and the messages are prose written for a chemist ("cannot read the safety table
  rules.yaml at …" vs "cannot screen component 4 of 9: …"). The agent on the other side sees both as
  the same kind of refusal — `mcp_server_kit/app.py:88-110` passes any `ValueError` through verbatim
  and replaces everything else — so a server whose hazard table failed its checksum produces a
  refusal an agent will read as "fix your input and retry", and the operator learns nothing.

  The engine docstring's claim — *"`SafetyRulesError`, the one exception type this engine raises"* —
  is also not true: `ich.py:187-203` (`_register`) and `reagents.py:80-119` (`_index`) raise bare
  `ValueError`, and `chem.py:28` raises `InvalidSmilesError`. A caller that writes
  `except SafetyRulesError` on the strength of that sentence does not cover the ICH collision path
  or the reagent-table path.
- **Evidence**: `screen.py:82-95` docstring — *"A screen cannot be performed: the input is unusable,
  **or** a rule table is missing/malformed"* — states the conflation as a design intent. Grep of the
  raise sites confirms both classes share the type; `ich.py:196` and `reagents.py:88,93,113` confirm
  the bare `ValueError`s.
- **Fix**: keep `SafetyRulesError` as the base (so every `except` clause and the `ValueError`
  passthrough are unchanged) and split two subclasses out of it — `SafetyInputError` for the three
  caller-fault raises and `SafetyCorpusError` for the table raises — then raise the corpus one from
  `ich._register` and `reagents._index` too, and correct the `engine/__init__.py` sentence to name
  the base rather than "the one exception type". Behaviour-preserving on the wire; it gives the
  caller and the metrics a type instead of a substring.

---

## `MAX_COMPONENTS` is an unvalidated module-global read at import time

- **Severity**: medium
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:74`
  (`MAX_COMPONENTS = int(os.environ.get("CHEMCLAW_SAFETY_MAX_COMPONENTS", "64"))`), consumed at
  `:362`
- **Trigger**: set `CHEMCLAW_SAFETY_MAX_COMPONENTS` to a non-integer, or to `0`.
- **Consequence**: two failure modes, neither of which the value's own comment (14 lines arguing the
  bound) covers.
  - Non-integer: the server fails at **import**, before `app` exists, with an unhandled `ValueError`
    from a module that is three imports deep. The pod crashloops and `/healthz` never answers; the
    traceback names `int()`, not the variable's purpose or the server.
  - `0` (or any value below 1): every multi-component screen is refused, and the refusal blames the
    caller — *"Screen a reaction's own species, not a library"* — so a hazard-screening server that
    has been misconfigured into screening nothing reads, to the agent and to the chemist, as though
    the request was wrong.
- **Evidence** (`/tmp/audit/d.py`):

  ```
  D1 rc: 1 | tail: ValueError: invalid literal for int() with base 10: 'sixty-four'
  D2 rc: 1 | tail: chemclaw_mcp_safety.engine.screen.SafetyRulesError: a hazard screen accepts at
      most 0 components, got 2. Screen a reaction's own species, not a library: ...
  ```

- **Fix**: parse it in one small function that validates and names itself, e.g.

  ```python
  def _max_components() -> int:
      raw = os.environ.get("CHEMCLAW_SAFETY_MAX_COMPONENTS", "64")
      try:
          value = int(raw)
      except ValueError:
          raise SafetyRulesError(
              f"CHEMCLAW_SAFETY_MAX_COMPONENTS must be a positive integer, got {raw!r}"
          ) from None
      if value < 1:
          raise SafetyRulesError(...)
      return value
  ```

  Behaviour-preserving for every valid value; it turns a crashloop and a silent
  screen-nothing configuration into one named, readable fault.

---

## `screen_reaction` parses and canonicalises every component twice

- **Severity**: low
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:420` —
  `flags = [flag for smiles in molecules for flag in screen_structure(smiles).flags]`
- **Trigger**: any multi-component screen.
- **Consequence**: `parse_components` (`:419`) has already parsed every component and holds the
  `Mol`. `screen_structure` then re-parses the same string through `parse_molecule` and re-runs
  `Chem.MolToSmiles` to build a `ScreenResult` whose `screened` field is thrown away one line later.
  Exactly 2x the parses and 2x the canonicalisations, all discarded — measured, not estimated — on
  the path this module's own docstring says is CPU-bound enough to need `asyncio.to_thread`.
- **Evidence** (`/tmp/audit/b.py`, wrapping `Chem.MolFromSmiles`/`MolToSmiles` with counters):

  ```
  components: 60 MolFromSmiles calls: 120 MolToSmiles calls: 120
  20x screen_reaction(60): 0.416 s
  ```

- **Fix**: extract `_structural_flags(as_written: str, molecule: Chem.Mol, table, patterns) ->
  list[HazardFlag]` — the comprehension currently inside `screen_structure` — and have
  `screen_structure` and `screen_reaction` both call it with the molecule they already hold.
  Behaviour-preserving (identical flag lists; `screen_structure`'s public signature is unchanged).

---

## `VapourPressure.pressure_kpa` is dead

- **Severity**: low
- **Location**: `servers/props/src/chemclaw_mcp_props/engine/correlations.py:52-56`
- **Trigger**: none — nothing calls it.
- **Consequence**: a property carrying a justification ("for plant instrumentation that reads in
  kPa") that no caller exists for. `tools.py`'s `VapourPressureResult` (`:69-80`) exposes mbar, bar
  and mmHg and never kPa; `VapourPressure` is an internal dataclass, never serialised, so the
  property cannot reach a client by any dynamic path either. Checked for dynamic use: there is no
  MCP registration, decorator, entry point or `getattr` path to it — the only registration surface
  in this server is `@server.tool()` in `tools.py`, whose six functions match
  `servers/props/connector.yaml:34-40` exactly.
- **Evidence**: repo-wide grep for `pressure_kpa` returns one hit, the definition itself.
- **Fix**: delete the property. If a kPa figure is actually wanted, it belongs as a field on
  `VapourPressureResult` where the agent can see it.

---

## `records.require` promises "the closest spellings" and returns the alphabetical first eight

- **Severity**: low
- **Location**: `servers/props/src/chemclaw_mcp_props/engine/records.py:167-181`
- **Trigger**: `records.require("dichlormethane")` — a single-letter typo of a solvent that *is* in
  the table.
- **Consequence**: the docstring says *"raising the message the agent should see when the name is
  unknown … with the closest spellings the table does know, so the next call can succeed."* The code
  is `sorted(...)[:8]` — alphabetical, unrelated to the query. For the typo above the message
  suggests eight names, none of them dichloromethane, and the model must call `list_solvents`
  anyway, which is the round trip the message was written to avoid.
- **Evidence**:

  ```
  'dichlormethane' is not in the vendored solvent table (44 solvents; e.g. 1,2-dichloroethane,
  1,4-dioxane, 1-butanol, 1-propanol, 2-methyltetrahydrofuran, 2-propanol,
  N,N-dimethylacetamide, N,N-dimethylformamide, ...). Call list_solvents to see the full set — ...
  ```

- **Fix**: either make the claim true — rank the candidate names by `difflib.get_close_matches`
  against `_key(name)` over every spelling in `_index()[1]`, which finds `dichloromethane` here —
  or reword the docstring to "a sample of the table" and keep the alphabetical slice. Making it true
  is four lines and no new dependency; leaving a false claim in a message the model is told to trust
  is the worse option.

---

## `resolve_compound_name` reports a match for any parseable SMILES, contradicting its "conservative" claim

- **Severity**: low
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/reagents.py:121-152`; module claim at
  `:29-33`; consumer at `servers/safety/src/chemclaw_mcp_safety/engine/ich.py:286-291`
- **Trigger**: `reagents.resolve_compound_name("CCCCCCCC")` (n-octane — parseable, not in the
  table).
- **Consequence**: the module docstring says *"Resolution is **conservative**: an unknown name
  returns no match rather than a guess. That property is the whole reason the ICH lookup may use
  this at all."* For structures it is not: the SMILES branch returns a non-`None` `ResolvedCompound`
  whose `name` is the query string itself. The current consumer survives because it re-folds that
  name and looks it up again — which on this branch is provably the *same key that already missed*,
  i.e. a guaranteed second miss — but the contract as written invites the next caller to treat
  `.name` as a recognised identity.

  The same function also carries three fields (`query`, `smiles`, `source`) that no code in this
  server reads; only `.name` is consumed. That is inherited from
  `servers/chem/src/chemclaw_mcp_chem/engine/reagents.py:44-52`, where the model *is* the MCP tool
  result and all four fields are used. The copy is defensible (one server never imports another);
  carrying chem's wire model into a server that has no wire for it is the part that is not.
- **Evidence** (`/tmp/audit/c.py`):

  ```
  'CCCCCCCC'   limit=None   resolve=('CCCCCCCC', 'smiles')
  fold('CCCCCCCC') == cccccccc
  resolved name: CCCCCCCC -> fold: cccccccc      # identical to the key that already missed
  ```

- **Fix**: in *this* server, narrow it to what its one caller needs — `resolve_compound_name(name)
  -> str | None` returning the table's display name, `None` when nothing was recognised (both the
  unknown-name and the unknown-structure cases). `impurity_limit` then reads
  `table.get(_fold(resolved))` with no dead branch, `ResolvedCompound` and the unread fields go, and
  the "conservative" sentence becomes true of the code beneath it. Behaviour-preserving: the only
  outcome that changes is the guaranteed-miss lookup, which stops happening.

---

## Public surface justified by callers that do not exist

- **Severity**: low
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:283-310` (`parse_molecule`,
  exported at `:48`); `servers/safety/src/chemclaw_mcp_safety/tools.py:76-78`
- **Trigger**: read the justifications, then grep for the callers.
- **Consequence**: two small pieces of structure whose stated reason for existing is not the case.
  - `parse_molecule`'s docstring: *"Public because the genotoxicity alert screen must fail the same
    way on the same input; a second parser there would be a second place for 'unparseable' to mean
    'clean'."* `genotox.py:33-38` imports `compile_smarts`, `parse_components`, `read_table` and
    `require_screenable_size` — **not** `parse_molecule`. The sharing is real but goes through
    `parse_components`; the only callers of `parse_molecule` are `screen_structure:382` and
    `parse_components:331`, both in the same module.
  - `tools.py:76-78` branches `if len(smiles) == 1: screen_structure(...) else screen_reaction(...)`.
    The two are output-identical for a one-element list, so the branch is a dispatch the tool layer
    does not need to make.
- **Evidence**: repo-wide grep for `parse_molecule` returns hits only inside `screen.py` (plus its
  own docstrings). Equivalence of the branch, run over four inputs including a pair-rule structure:

  ```
  CCO identical: True
  CN=[N+]=[N-] identical: True
  OO identical: True
  c1ccccc1[N+](=O)[O-] identical: True
  ```

- **Fix**: make `parse_molecule` private (`_parse_molecule`, drop it from `__all__`) and correct its
  docstring to say the sharing happens via `parse_components`; replace the `tools.py` branch with a
  single `await asyncio.to_thread(screen_reaction, smiles)`. Both behaviour-preserving on the
  payload — the one visible difference is that a malformed single SMILES would be refused as
  "component 1 of 1" instead of "the structure given", so if that wording is worth keeping, keep the
  branch and delete the claim instead.

---

## Three hand-written projections of `Solvent` in `props/tools.py`

- **Severity**: low
- **Location**: `servers/props/src/chemclaw_mcp_props/tools.py:29-38` (`SolventSummary`),
  `:40-66` + `:148-173` (`SolventRecord` / `_record`), `:123-138` + `:424-439` (`ComparisonRow`)
- **Trigger**: rename or add a column in `data/records.csv`.
- **Consequence**: every field of the table is renamed on the way out (`bp_c` ->
  `boiling_point_c`, `mw` -> `molecular_weight`, `density_20c` -> `density_20c_g_per_ml`, …) and the
  renaming is written out three times across two wire models plus one inline construction.
  `ComparisonRow` is `SolventRecord` minus seven fields, and `compare_solvents` rebuilds it field by
  field at `:424-439` rather than reusing `_record`. Adding one property to the table means editing
  the dataclass, the CSV reader, and two or three of the projections — with nothing failing if one
  is missed, because each model is independently valid.
- **Evidence**: `bp_c` is projected at `:157` (`boiling_point_c`), `:194` (`bp_c`) and `:427`
  (`boiling_point_c`) — three sites, two names, one source field.
- **Fix**: keep `SolventRecord` and `_record` as the single projection, and define the other two as
  narrowings of it rather than as siblings — either build the full record and select fields
  (`SolventSummary.model_validate(record.model_dump())` with the summary declaring only its six
  fields, and the same for `ComparisonRow`), or drop `ComparisonRow` entirely and have
  `compare_solvents` return `list[SolventRecord]`. The first is behaviour-preserving on the wire;
  the second changes the comparison payload and should only be taken if the extra fields are wanted.
