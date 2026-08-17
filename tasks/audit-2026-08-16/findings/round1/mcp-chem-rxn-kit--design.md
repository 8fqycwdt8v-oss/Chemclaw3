# Design & simplification review — `servers/chem`, `servers/rxnpredict`, `packages/mcp_server_kit`, `manifests/`, Makefile + pyproject

Repo: `/workspace/chemclaw3-mcp`. Every claim below was run, not reasoned about; commands and
output are recorded inline.

---

## `manifests/` is the discovery directory *and* contains the one bundle that must not be discovered

- **Severity**: high
- **Location**: `/workspace/chemclaw3-mcp/manifests/README.md` (the `export CHEMCLAW_CONNECTORS_DIR=...` recipe) and `/workspace/chemclaw3-mcp/manifests/calc/connector.yaml` (symlink); enforced by `/workspace/chemclaw3-mcp/tests/test_fleet.py:70` (`test_the_manifest_is_registered_by_symlink`)
- **Trigger**: an operator follows this repo's own instruction —
  `export CHEMCLAW_CONNECTORS_DIR="/path/to/Chemclaw3-mcp/manifests:<core connectors dir>"` — and
  starts Chemclaw3.
- **Consequence**: Chemclaw3's discovery is "any subdirectory holding a `connector.yaml`, first
  directory on the path wins, and an empty `connectors_enabled` means *every* discovered bundle"
  (`/home/user/Chemclaw3/src/chemclaw/connectors/registry.py:108-125`, `:166-175`). `manifests/calc/`
  is such a subdirectory, so `calc` resolves to *this* repo's manifest and shadows the core bundle.
  The two are not equivalent: the core `calc` bundle declares **five durable jobs**
  (`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`,
  `compute_interaction_energy`) and this repo's declares **none**. Those five job tools vanish from
  the agent's surface with no error, no warning and no failed startup — the manifest is valid, it
  simply describes a smaller capability under the same name.
- **Evidence**: the repo already knows this. `manifests/README.md`:

      **`calc` is a third entry carrying a Chemclaw3 bundle's name, and it must *not* be registered
      this way.** … Putting this directory on `CHEMCLAW_CONNECTORS_DIR` would let a partial port win
      the name and take the calibration ledger, the calculation cache, the artifact store and every
      durable calc job off the agent's surface — **with no error**.

  The whole protection is that paragraph, in the same file whose first code block tells you to point
  `CHEMCLAW_CONNECTORS_DIR` at the directory. Measured:

      $ grep -c "^jobs:" servers/calc/connector.yaml            → 0
      $ grep -c "^  - name:" /home/user/Chemclaw3/src/chemclaw/connectors/calc/connector.yaml → 5
      $ ls -la manifests/calc/
      connector.yaml -> ../../servers/calc/connector.yaml

  And `tests/test_fleet.py:70` *requires* the symlink to exist for every server directory, so the
  test suite actively enforces the hazardous state; removing `manifests/calc/` to fix this turns the
  fleet test red.
- **Fix**: stop overloading one directory with two jobs. Split the discovery surface from the
  "every server has a manifest" invariant:
  - keep `manifests/` as *only* what an operator may point at — `chem`, `props`, `rxnpredict`,
    `safety`;
  - move `calc`'s registration to a sibling that discovery would never be pointed at (e.g.
    `manifests-internal/calc/connector.yaml`), and
  - change `test_the_manifest_is_registered_by_symlink` to look the manifest up in a per-server
    declared location (a `registry:` field in the server's own `connector.yaml`, or a small table in
    `tests/test_fleet.py` naming which directory each server registers into) so the invariant "one
    manifest, symlinked, never copied" is preserved while the directory an operator exports stops
    carrying a bundle that must not be exported.

  Behaviour-preserving for the four servers that *are* meant to be discovered; deliberately
  behaviour-changing for `calc`, which is the point.

---

## `make type` does not type-check `servers/rxnpredict/` — 28 files — while claiming it checks every server

- **Severity**: medium
- **Location**: `/workspace/chemclaw3-mcp/Makefile:4` (`SRC := …`) and `:26` (the `type` target's help text); relied on by `/workspace/chemclaw3-mcp/.github/workflows/ci.yml` ("Types" step)
- **Trigger**: run `make type` (or CI's Types step) with any type error in
  `servers/rxnpredict/src/`.
- **Consequence**: the gate is green. `servers/rxnpredict` is the largest single body of code in the
  repo (2,934 LOC, 28 modules, the whole predictor fleet and the aggregator) and no one has ever
  run `mypy --strict` over it in CI. The same omission drops it from the `run-<name>` targets:
  `run-props`, `run-chem`, `run-safety`, `run-calc` exist; `run-rxnpredict` does not.
- **Evidence**: the `SRC` list names five trees and skips rxnpredict:

      SRC := packages/mcp_server_kit/src servers/props/src servers/chem/src servers/safety/src servers/calc/src

  while the target's help says `## mypy --strict over every server and the shared kit.` I appended a
  blatant error to `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/preprocessing.py`
  (`def _audit_probe(x: int) -> str: return x`) and ran both commands:

      == make type ==            (the exact SRC list)
      Success: no issues found in 59 source files
      == explicit rxnpredict ==
      .../preprocessing.py:70: error: Incompatible return value type (got "int", expected "str")
      Found 1 error in 1 file (checked 28 source files)

  (probe reverted; `git status --porcelain` clean.) The rxnpredict tree happens to be clean today —
  `mypy servers/rxnpredict/src` → `Success: no issues found in 28 source files` — so nothing is
  currently broken by it; what is broken is the gate. Note the CI comment directly above the step,
  which is a claim contradicted by the code it introduces: *"`make type` rather than a repeated path
  list: this step had already drifted … One list, in the Makefile, is what stops that recurring as
  servers are added."* One list did not stop it; rxnpredict is the server that drifted out.
- **Fix**: derive the list instead of maintaining it — `SRC := $(wildcard packages/*/src)
  $(wildcard servers/*/src)` — and add a `tests/test_fleet.py` assertion that every
  `servers/*/src` appears in the `type` target's expansion, the same both-directions check the rest
  of that file already does for manifests, ports and MODULES.md. Add `run-rxnpredict` for symmetry
  (or generate the five run targets from one pattern rule). Behaviour-preserving.

---

## The shared kit ships its own test harness into every server image, and it is the one module holding an HTTP client that the no-egress scan never looks at

- **Severity**: medium
- **Location**: `/workspace/chemclaw3-mcp/packages/mcp_server_kit/src/mcp_server_kit/testing.py` (whole module; `served_tools` at :33), `.../no_egress.py`, and the scan's call sites `servers/*/tests/test_no_egress.py`
- **Trigger**: build any server wheel, or run the repo's own AST egress scan over the kit.
- **Consequence**: three things at once.
  1. `testing.py` and `no_egress.py` are pure test tooling and are packaged into the runtime wheel
     that goes into every server image.
  2. `testing.py` imports `httpx` and `yaml` at module scope. `httpx` is on this repo's own
     `FORBIDDEN_MODULES` list, and the static scan is only ever pointed at each *server's* package
     (`assert_no_egress_sources(PACKAGE)` where `PACKAGE = Path(chemclaw_mcp_chem.__file__).parent`)
     — never at the kit. So the one first-party module in the fleet that imports an HTTP client is
     the one module no check covers, and `no_egress.py`'s docstring claim ("our own code imports no
     way to call out") is unverified for the package that *defines* the check. `yaml` is not even a
     declared dependency of `mcp-server-kit` (`fastapi, mcp, prometheus-client, pydantic, starlette`
     — pyyaml arrives only through the workspace dev group), so the module is unimportable in a real
     image.
  3. `served_tools` — documented as *"the only honest input to the manifest mirror test"* — has
     **zero callers**. All five servers instead hand-roll the same uvicorn harness.
- **Evidence**:

      $ python -c "from mcp_server_kit.no_egress import assert_no_egress_sources; ..."
      FAILS: servers in this repository answer from vendored data and never call out:
        .../mcp_server_kit/egress.py: imports socket
        .../mcp_server_kit/testing.py: imports httpx

      $ uv build --wheel --out-dir /tmp/kitwheel packages/mcp_server_kit
      ['mcp_server_kit/__init__.py', 'mcp_server_kit/app.py', 'mcp_server_kit/auth.py',
       'mcp_server_kit/datasets.py', 'mcp_server_kit/egress.py', 'mcp_server_kit/identity.py',
       'mcp_server_kit/no_egress.py', 'mcp_server_kit/testing.py', ...]

      $ grep -rn "served_tools" servers packages tests scripts --include="*.py"
      packages/mcp_server_kit/src/mcp_server_kit/testing.py:3   (docstring)
      packages/mcp_server_kit/src/mcp_server_kit/testing.py:22  (__all__)
      packages/mcp_server_kit/src/mcp_server_kit/testing.py:33  (the def)
      # no call sites

  The duplication `served_tools` was meant to remove is real and measurable: the `running_server`
  fixture, `_free_port` and `_session` are byte-identical across all five `tests/test_server.py`
  apart from the token env var, the app import and one failure message —

      $ diff <(sed -n '/^def _free_port/,/^def test_healthz/p' servers/chem/tests/test_server.py) \
             <(sed -n '/^def _free_port/,/^def test_healthz/p' servers/rxnpredict/tests/test_server.py)
      # differences: docstring, CHEMCLAW_CHEM_TOKEN→CHEMCLAW_RXNPREDICT_TOKEN, the app import,
      # the fixture's two registry lines, one pytest.fail message. ~35 identical lines × 5 servers.
- **Fix**: move the harness out of the runtime package and finish the abstraction it was started for.
  Add `packages/mcp_server_kit_testing/` (a dev-group-only distribution, not a dependency of any
  server) holding `load_manifest`, `assert_manifest_matches`, `served_tools`, and a new
  `running_server(app, token_env, token)` pytest fixture factory carrying the uvicorn thread,
  `_free_port` and the ready-poll. Each server's `tests/test_server.py` then declares three lines
  instead of sixty. Delete `mcp_server_kit/testing.py`; move `no_egress.py` there too (it is only
  ever called from tests). Then point the per-server `test_no_egress.py` at the kit as well, so the
  static claim covers the code that makes it. Behaviour-preserving for the served surface; it only
  moves test code.

---

## `disabled_models` does not disable a model on two of the six tools

- **Severity**: medium
- **Location**: `/workspace/chemclaw3-mcp/servers/rxnpredict/src/chemclaw_mcp_rxnpredict/tools.py:293` (`predict_forward_single_model`) and `:324` (`predict_conditions_single_model`); policy defined at `:92` (`_select`), `:102`, `:114`
- **Trigger**: `CHEMCLAW_RXNPREDICT_DISABLED_MODELS=megan`, then the agent calls
  `predict_forward_single_model(model_name="megan", …)`.
- **Consequence**: the ensemble tools honour the switch and the single-model tools do not — they
  search `list_forward()` / `list_conditions()` directly, bypassing both `disabled_models` and the
  `enabled_*_models` allowlist. A deployment that turns off a predictor because it is broken,
  mis-licensed for that site, or too slow still serves it: all six tools are `read_only` in the
  manifest, so the plan gate lets an unapproved plan reach it. The `Settings` field description
  ("Comma list of predictor IDs to force off") and `parse_disabled`'s docstring ("The predictor IDs
  forced off, **whatever the enabled list says**") are both claims the code does not keep.
- **Evidence**: `/tmp/probe_disabled.py`, run against the deterministic doubles with
  `CHEMCLAW_RXNPREDICT_DISABLED_MODELS=fake_a` and `fake_a` the only registered forward predictor:

      ENSEMBLE refused (disabled respected): no forward predictors are available in this deployment…
      SINGLE-MODEL answered from the DISABLED predictor: ['CC(=O)Nc1ccccc1']
- **Fix**: the selection policy belongs in one function, not in two of four call sites. Add
  `enabled_forward(requested=None)` / `enabled_conditions(requested=None)` to
  `engine/predictors/__init__.py` (which is already the module that owns the registry), have the two
  ensemble tools call it with the caller's subset and the two single-model tools call it with
  `[model_name]`, so a disabled name produces the existing "no predictor named … is loaded" error.
  Behaviour-changing on the single-model tools by design; the fix is the behaviour change.

---

## Four copies of "canonicalise, or fall back to the raw string", two of which disagree — and the function written to unify them is dead

- **Severity**: medium
- **Location**: clone sites —
  `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/tools.py:62` (`_safe_canon_reactants`),
  `tools.py:70` (`_safe_canon_single`),
  `.../engine/cache.py:44` (`_safe_canon_reactants`),
  `.../engine/cache.py:52` (`_safe_canon_product`),
  `.../engine/meta/aggregator.py:44` (`_normalise_product`) and the same try/except inlined again at
  `aggregator.py:111` (`_canon_set`). The unused unifier is
  `.../engine/preprocessing.py:53` (`canonical_reaction_input`).
- **Trigger**: call `predict_forward_reaction(reactants="Nc1ccccc1.CC(=O)Cl>CCN(CC)CC>")` — the
  full `reactants>agents>` form the tool's own docstring says is accepted — then call it again with
  the reactants written the other way round.
- **Consequence**: two same-named functions, different behaviour. `tools._safe_canon_reactants`
  splits on `>` first and canonicalises the reactant half; `cache._safe_canon_reactants` does not,
  so `canonical_multi_smiles` splits the whole string on `.`, RDKit refuses the `>`-bearing
  fragment, the broad `except Exception` swallows it and the **raw text becomes the cache key**. The
  cache's module docstring — *"The key is … predictor, *canonical* reactants (and product), top_k —
  so two spellings of one reaction share a slot"* — is therefore false for exactly the input form
  the tool advertises. Every reaction-SMILES call is a guaranteed cache miss, and each miss re-runs
  a T5 checkpoint. Secondary: RDKit writes a parse-error block to stderr on every such lookup.
- **Evidence**:

      tools : 'CC(=O)Cl.Nc1ccccc1'
      cache : 'Nc1ccccc1.CC(=O)Cl>CCN(CC)CC>'      # raw, uncanonicalised
      tools2: 'CC(=O)Cl.Nc1ccccc1'
      cache2: 'CC(=O)Cl.Nc1ccccc1>CCN(CC)CC>'      # raw, uncanonicalised
      same-reaction, other spelling hits cache?  False     ← with the agent segment
      no-agent form, other spelling hits cache?  True      ← without it

  And `canonical_reaction_input`, whose docstring argues precisely this case —

      Unlike taking only `s.split(">")[0]`, this keeps reagent/agent context distinct so two
      chemically different inputs (e.g. `A.B>reagent>` vs `A.B`) do not collapse to the same string.

  — has no production caller at all:

      $ grep -rn canonical_reaction_input servers packages scripts --include="*.py"
      .../engine/preprocessing.py:53   (the def)
      servers/rxnpredict/tests/test_preprocessing.py:12,49,50,57   (tests only)
- **Fix**: put two functions in `preprocessing.py` and delete the five private copies:
  `safe_canonical(smiles)` (single molecule, `except ValueError` → raw) and
  `safe_canonical_reaction(text)` = `canonical_reaction_input` with the same fallback. Point
  `cache._key_forward` / `_key_conditions`, `tools._safe_canon_*`, `aggregator._normalise_product`
  and `_canon_set` at them. Behaviour-preserving for plain dot-separated reactants; deliberately
  behaviour-changing for the `>`-bearing form, where it makes the cache do what its docstring says
  (and note that this shifts existing keys, which is free — the cache is in-process and lost on
  restart by design).

---

## `_forward_predictors` / `_conditions_predictors` are clones; `_select` declares a `None` it can never return

- **Severity**: low
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/tools.py:92-123`
- **Trigger**: read the two functions side by side.
- **Consequence**: 22 lines to express one selection rule twice. `_select` is annotated
  `-> set[str] | None` and every path returns a `set`, so the `(allowed or set())` guard in both
  callers is dead defensive code that exists only to satisfy an annotation nothing produces. Both
  callers also call `list_forward()` / `list_conditions()` twice per invocation. And the returned
  lists are typed `list[object]`, which forces eight `# type: ignore[attr-defined]` on
  `p.name`/`p.predict` at `:180`, `:185`, `:245`, `:251`, `:356-358` — while
  `engine/predictors/__init__.py:52` already returns a properly typed
  `list[BaseForwardPredictor]`. The annotation is throwing away type information that exists, and
  then suppressing the errors that loss creates.
- **Evidence**:

      $ python -c "from chemclaw_mcp_rxnpredict.tools import _select; print(_select(['fake_a'], None))"
      set()   <class 'set'>       # never None

  The two bodies differ only in `list_forward`↔`list_conditions` and
  `enabled_forward_models`↔`enabled_conditions_models`.
- **Fix**: one generic helper in the registry (see the `disabled_models` finding — the same refactor
  covers both), returning `list[BaseForwardPredictor]` / `list[BaseConditionsPredictor]`; drop
  `_select`'s `| None`, drop the `or set()` guards, and delete all eight `type: ignore`s.
  Behaviour-preserving.

---

## Registry module-globals with no public reset, and three dead accessors

- **Severity**: low
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/__init__.py:20-22` (`_FORWARD`, `_CONDITIONS`, `_UNAVAILABLE`), `:44` (`get_forward`), `:48` (`get_conditions`), `:64` (`_DISCOVERY_DONE`); `engine/predictors/base.py:34` (`is_loaded`); import-time call at `tools.py:59`
- **Trigger**: write a test that needs a known predictor set.
- **Consequence**: there is no supported way to scope the registry, so five test sites reach into
  the private dict — `servers/rxnpredict/tests/conftest.py:41,42,58,60`,
  `tests/test_server.py:50,72`, `tests/test_tools.py:105` all call `registry._FORWARD.clear()` and
  `registry._FORWARD.update(saved)`. Meanwhile `get_conditions` has **no caller anywhere**,
  `get_forward` is called only from a test, and `is_loaded()` has no caller at all. `tools.py`
  calls `discover_predictors()` as an import side effect, so importing the tool module imports and
  probes eleven optional ML stacks.
- **Evidence**: `grep -rn "get_conditions\|is_loaded" servers packages scripts --include="*.py"`
  returns, for the registry symbols, only their own definitions (the `rxn_insight.py:76` hit is a
  different string — an attribute name probed on the third-party object). The three `clear()` sites
  above are in the grep output verbatim.
- **Fix**: delete `get_forward`, `get_conditions` and `is_loaded` (nothing dynamic registers them —
  registration is the explicit `register_forward`/`register_conditions` calls listed in
  `_FORWARD_MODULES`/`_CONDITIONS_MODULES`, which I checked). Add one public
  `reset_registry_for_tests()` beside the existing `reset_cache_for_tests()` and
  `reset_settings_for_tests()`, which the repo already uses as its idiom, and point the five test
  sites at it. Behaviour-preserving.

---

## `connector_app` monkeypatches `manager.call_tool` twice, and `BodySizeLimit` carries a branch that cannot run

- **Severity**: low
- **Location**: `packages/mcp_server_kit/src/mcp_server_kit/app.py:58-110` (`_bind_caller_per_tool_call`, `_sanitize_tool_errors`); `packages/mcp_server_kit/src/mcp_server_kit/auth.py:159,167,174-179` (`refused`)
- **Trigger**: read `connector_app`'s two wrapper installers; and trace `_BodyTooLarge`.
- **Consequence**: two functions with identical structure (fetch `_tool_manager`, guard `None`,
  capture `wrapped`, define `async def call_tool`, reassign) each independently coupled to the same
  private MCP attribute and each with its own `pragma: no cover` branch for the day upstream renames
  it. The ordering between them is load-bearing and expressed only as a comment at `:177-178`.
  Separately, `refused` in `BodySizeLimit.__call__` is a flag that can only ever be `True` when it
  is read: `_BodyTooLarge` is module-private and raised at exactly one site, on the line immediately
  after `refused = True`. So `if refused:` is always taken and the `raise` at `:179` is unreachable.
- **Evidence**: all six occurrences of `refused`/`_BodyTooLarge` in `auth.py`:

      159:  refused = False
      162:      nonlocal seen, refused
      167:                  refused = True
      168:                  raise _BodyTooLarge
      173:      except _BodyTooLarge:
      174:          if refused:
      182:  class _BodyTooLarge(Exception):

  Nothing else can construct or raise `_BodyTooLarge`.
- **Fix**: collapse the two installers into one `_wrap_tool_calls(server, name=name)` that grabs
  `_tool_manager` once, checks it once, and installs a single wrapper doing bind → try/except →
  reset in the order the comment currently describes — making the ordering structural instead of
  documented, and leaving one place to fix when upstream exposes a middleware hook. Delete
  `refused` and the unreachable `raise`. Both behaviour-preserving.

---

## `chem`'s charge table resolves every solvent twice

- **Severity**: low
- **Location**: `servers/chem/src/chemclaw_mcp_chem/engine/stoichiometry.py:151` and `:154` (`_solvent_row`), against `engine/reagents.py:156` (`density_of`)
- **Trigger**: any `stoichiometry_table` call with a `solvents`/`volumes` pair.
- **Consequence**: `_solvent_row` calls `resolve_compound_name(solvent)`, then immediately calls
  `density_of(solvent)`, which resolves the *same* string again from scratch — a second normalise,
  a second table lookup and, for a solvent given as a SMILES, a second RDKit parse and
  canonicalisation. Two RDKit parses per solvent on a path whose module docstring is explicit that
  it batches the whole table into one worker-thread hop precisely because parses are the cost.
- **Evidence**: spying on `resolve_compound_name` for a table with two solvents given as SMILES:

      resolve_compound_name calls: ['AcOH', 'TEA', 'C1CCOC1', 'C1CCOC1', 'CO', 'CO']
      species charged: 4

  Reagents are resolved once; each solvent twice.
- **Fix**: `_solvent_row` already holds `match`, so read the density off the structure it just
  resolved — `density = _index()[2].get(match.smiles)` — or, to keep `_index` private to
  `reagents.py`, add `density_for_smiles(canonical: str) -> float | None` there and let
  `density_of(name)` be `resolve → density_for_smiles`, which makes the two-caller split honest
  instead of duplicated. Behaviour-preserving (`density_of` resolves the same string to the same
  `match.smiles`).

---

## The manifest classification rule is asserted in two places, one of which claims to be the only one

- **Severity**: low
- **Location**: `packages/mcp_server_kit/src/mcp_server_kit/testing.py:56-82` (`assert_manifest_matches`, check 3) and `tests/test_fleet.py:81-91` (`test_every_tool_is_classified_exactly_once`)
- **Trigger**: change the classification rule.
- **Consequence**: two implementations of "every tool is `read_only` xor `state_changing`", and they
  are not the same — the fleet version additionally asserts `(read_only | state_changing) - tools`
  (classified-but-unserved), which the kit version omits. The kit's own module docstring says the
  check lives there *"rather than copied into each server's tests so it cannot drift into seven
  slightly different assertions"*, and there is already a second, slightly different assertion.
- **Evidence**: `testing.py:77-82` vs `test_fleet.py:85-91` — same three set operations, one extra
  assertion in the fleet copy, different failure messages.
- **Fix**: `test_fleet.py` calls the kit function (passing the manifest's own `tools` list as the
  served names, which is what it is already comparing against), and the missing
  classified-but-unserved assertion moves *into* `assert_manifest_matches` so both callers get it.
  One implementation, strictly stronger. Behaviour-preserving apart from the extra check now
  applying to the per-server tests too — which is the correct direction.

---

## What I looked at and did not find a problem with

`mcp_server_kit/datasets.py`, `identity.py` and `egress.py` are each one responsibility, no
single-caller indirection, no hardcoded config; `egress`'s module-globals are inherent to a process-
wide socket patch. `chem/engine/chem.py`, `depiction.py` and `reagents.py` are small, single-purpose
and their comments match their code (I checked the three `require_molecule` rejection cases and the
`_reaction` claims against the installed RDKit). `chem/tools.py`'s per-tool
`to_thread`/no-`to_thread` split is consistent with what each tool actually does.
`aggregator.py`'s two `aggregate_*` functions look like clones but genuinely differ (the condition
key, the temperature bucketing and the mean-temperature bookkeeping), so folding them would cost
more than it buys. No dead MCP tool: all six `@server.tool()` names in `rxnpredict` and all four in
`chem` appear in their manifests and are exercised by `tests/test_server.py`.
