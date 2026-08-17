# core kernel — design and simplification

Slice: `src/chemclaw/core/config/*.py`, `core/errors.py`, `core/ids.py`, `core/bounded.py`,
`core/chem.py`, `core/reagents.py`.

Read in full. `core/bounded.py`, `core/ids.py` and `core/errors.py` are clean under this lens and
produced nothing — `BoundedLru` has five real callers and no hand-rolled `OrderedDict` survives
anywhere in `src/` (verified), every one of its six methods is used, and `stable_hash` has 54
first-party call sites. The findings below are all in `core/config/` and `core/reagents.py`.

---

## Three dead Entra settings, one of them set to a real-looking tenant URL by the shipped chart

- **Severity**: high
- **Location**: `src/chemclaw/core/config/entra.py:74-76` — `entra_token_endpoint`,
  `entra_sa_token_path`, `entra_token_refresh_leeway_seconds`
- **Trigger**: an operator follows `deploy/helm/chemclaw/values.yaml:385`, which ships
  `CHEMCLAW_ENTRA_TOKEN_ENDPOINT: "https://login.microsoftonline.com/TENANT/oauth2/v2.0/token"`
  under the heading "Entra identity is mandatory system-wide", and substitutes their tenant. Or
  they set `CHEMCLAW_ENTRA_SA_TOKEN_PATH` from `.env.example:754` to move the projected
  ServiceAccount token.
- **Consequence**: nothing happens. No code reads any of the three. The operator has configured a
  token-minting path that does not exist, and there is no error, warning or metric that says so.
  The neighbouring `entra_http_timeout_seconds` *is* live (`api/auth.py:83`), so the block reads as
  a working group of four.
- **Evidence**: AST scan over every `.py` in `src/` and `tests/` (excluding the declaring module)
  for any `Name` or `Attribute` node with these identifiers:

  ```
  entra_token_endpoint              -> NO CODE REFERENCE ANYWHERE
  entra_sa_token_path               -> NO CODE REFERENCE ANYWHERE
  entra_token_refresh_leeway_seconds-> NO CODE REFERENCE ANYWHERE
  ```

  The only textual hits for `entra_token_endpoint` in `src/` are `core/logging.py:446`, where it is
  named *as an example string in a comment* about the redaction inventory.

  The comment above the fields concedes the mechanism was deleted ("The code that did it … is
  gone") and then keeps the fields "because they describe the tenant … and whatever mints a token
  here next will need exactly them". That is a for-later stub, and this repository has already
  written the counter-argument for a different section: `core/config/calculators.py:8-16` states
  that a setting with no reader "was silent in the worst way available", and
  `tests/test_config.py::test_no_calculator_setting_is_declared_without_a_reader` fails the build
  for it — but only for `calculators.py`. The identical defect one file over is shipped in the
  chart with a plausible value filled in.
- **Fix**: delete the three fields, their `.env.example` rows (753-755) and the `values.yaml` key
  (385). Behaviour-preserving: yes — nothing reads them, and `extra="forbid"` is on the composed
  `Settings`, so the only visible change is that a deployment still passing the env var now fails
  loudly at startup instead of silently doing nothing, which is the correct outcome. Then widen
  `test_no_calculator_setting_is_declared_without_a_reader` to scan the whole `core/config/`
  package with an explicit allow-list for fields a chart or library legitimately consumes — the
  test is already 90% general and is scoped to one file by an `assert` on one path.

---

## `density_of` and its 22-entry table are dead code in this repository

- **Severity**: medium
- **Location**: `src/chemclaw/core/reagents.py:174-217` (`_RAW_DENSITIES` plus a 20-line comment),
  `:284-300` (`_index_densities`, `_DENSITY_BY_STRUCTURE`), `:366-379` (`density_of`)
- **Trigger**: import `chemclaw.core.reagents` anywhere. `_index_densities()` runs at import,
  re-keys 22 densities onto canonical SMILES, and builds a map nothing ever reads.
- **Consequence**: ~75 lines of chemistry data and index-building code with zero consumers — not in
  `src/`, not in `tests/`. Worse, the comment block that justifies them (lines 187-193) argues from
  the behaviour of `stoichiometry_table` and `green_metrics`, and **neither function exists in this
  repository** — both moved to `Chemclaw3-mcp`. So a reader is told the table's cost was
  "measured", is given a 2.17x error figure, and the code that produced that figure is not here.
  Whether `Chemclaw3-mcp` now carries its own copy of the same 22 densities is the duplication
  question this leaves open; either way this copy is unreachable.
- **Evidence**:

  ```
  $ grep -rn "stoichiometry_table\|green_metrics" --include=*.py src/
  src/chemclaw/core/reagents.py:180: ... `stoichiometry_table` acted on
  src/chemclaw/core/reagents.py:187: ... `stoichiometry_table` took only molar equivalents, so
  src/chemclaw/core/reagents.py:193: ... `green_metrics` is computed from, so the same gap ...
  ```
  (three comment mentions, no definition, no call)

  AST scan for `density_of` over all of `src/` and `tests/`: `NO CODE REFERENCE ANYWHERE`.
  `_DENSITY_BY_STRUCTURE` has exactly one reader — `density_of` itself.
- **Fix**: delete `_RAW_DENSITIES`, `_index_densities`, `_DENSITY_BY_STRUCTURE` and `density_of`,
  and move the density table to whichever repository owns `stoichiometry_table` (it needs one copy,
  and this is not the repository that computes with it). Behaviour-preserving: yes for this
  repository — the deleted symbols have no caller. Check `Chemclaw3-mcp` first: if it does *not*
  already have the table, this is a cross-repo move rather than a delete.

---

## `known_names()` is kept alive only by a test, and its docstring describes a caller that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/core/reagents.py:342-344`
- **Trigger**: none — no production call path reaches it.
- **Consequence**: the docstring says it is "what a caller can offer as a suggestion on a miss", and
  `resolve_compound_name` (the only thing that produces a miss) never calls it, nor does any tool,
  route or middleware. Its two real uses are `tests/test_compound_identity.py:417,431` iterating the
  table as a fixture. This is the shape `CLAUDE.md` names for `reject_widening`: a public function
  whose only client is a test that calls it directly.
- **Evidence**: AST scan → `known_names -> ['tests/test_compound_identity.py:431', ':417']`,
  nothing in `src/`.
- **Fix**: either make it private (`_all_names()`) and let the two tests use it as the fixture
  accessor it actually is, or give `resolve_compound_name`'s miss path the suggestion the docstring
  promises. Behaviour-preserving either way; picking is a product decision, but the current state —
  a public API documented for a use it does not have — is neither.

---

## Three clones of "the selected backend requires these fields", one of them adjacent

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/llm.py:164-170` (`_llm_provider_config`),
  `llm.py:182-192` (`_embedding_provider_config`), `src/chemclaw/core/config/hpc.py:128-139`
  (`_hpc_launch_config`)
- **Trigger**: adding a fourth provider/backend that needs required fields.
- **Consequence**: the identical five-line body is written three times, twice in the same class
  eighteen lines apart. All three build a `tuple[tuple[str, str], ...]` named `required`, compute
  `missing = [name for name, value in required if not value]`, and raise
  `f"<selector>='<value>' requires {', '.join(missing)} to be set"`. `hpc.py`'s docstring even says
  "mirroring `_llm_provider_config`" — the coupling is acknowledged and not extracted, which is
  exactly the Rule-of-Three threshold this repository states it applies.
- **Evidence**: the three bodies, side by side:

  ```python
  # llm.py:165-170
  required = (("llm_base_url", self.llm_base_url), ("llm_model", self.llm_model))
  missing = [name for name, value in required if not value]
  if missing:
      raise ValueError(f"llm_provider='openai_compatible' requires {', '.join(missing)} to be set")

  # llm.py:183-192  (identical, two entries, different names)
  # hpc.py:129-139  (identical, four entries, different names)
  ```
- **Fix**: one helper beside `_shipped` (which is already the package's shared-utility module):

  ```python
  def require_fields(selector: str, required: Sequence[tuple[str, str]]) -> None:
      missing = [name for name, value in required if not value]
      if missing:
          raise ValueError(f"{selector} requires {', '.join(missing)} to be set")
  ```

  Behaviour-preserving: yes. The three call sites pass
  `"llm_provider='openai_compatible'"` / `"embedding_provider='openai_compatible'"` /
  `"hpc_launch_interface='nextflow'"` and the messages come out byte-identical. The two message
  assertions in `tests/test_config.py:207,309` match on the joined field names, which survive.

---

## Four of the six checks on the composed class are single-section rules, against the placement rule stated in the same file

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/__init__.py:150-255`
  (`_guards_that_the_comments_already_demand`), `:257-277`
  (`_the_fan_out_ceiling_covers_the_section_it_bounds`)
- **Trigger**: a maintainer reads the package docstring's rule — "A cross-field validator lives in
  the section that owns the relationship; only a rule that spans sections lives here on the composed
  class, because no single section can see both sides of it" (`__init__.py:23-25`) — and then
  looks for the pool/budget/admission guards in their sections. They are not there.
- **Consequence**: a 106-line validator function holding six unrelated checks, four of which do not
  span sections at all, so the one place a reader is told is reserved for cross-cutting rules is
  where most of `ServiceSettings`' and `StoreSettings`' own invariants live. Concretely:

  | check | fields read | section |
  |---|---|---|
  | `service_uvicorn_workers > 1` | 1 field | `ServiceSettings` only |
  | fleet admission product | `service_fleet_replicas`, `service_uvicorn_workers`, `service_max_concurrent_turns`, `service_fleet_max_concurrent_turns` | `ServiceSettings` only |
  | fleet connection product | `pg_fleet_pooled_processes`, `pg_pool_max_size`, `pg_fleet_max_connections` | `StoreSettings` only |
  | budgets-all-zero | `budget_enabled`, four `budget_max_*` | `ServiceSettings` only |
  | mid-turn resume | `mid_turn_resume_*` (Memory) + `service_turn_timeout_seconds` (Service) | genuinely cross |
  | embedding width | `embedding_dim` (Llm) + `data_sources` (Sources) + `note_reindex_enabled` (Retrieval) | genuinely cross |

  `_the_fan_out_ceiling_covers_the_section_it_bounds` reads `fan_out_child_timeout_seconds` and
  `report_section_timeout_seconds`, **both** in `ReportSettings`, and its own docstring concedes
  it: "both settings live in that section, so this one could move".
- **Evidence**: field ownership read directly from the section modules
  (`service.py:55,105,128,129,196-200`, `store.py:85,101,102`, `reports.py`). Mixin-level
  `model_validator(mode="after")` demonstrably fires on the composed class, so the move is
  mechanical:

  ```
  $ uv run python -c "from chemclaw.core.config import Settings; Settings(_env_file=None, pg_pool_min_size=20, pg_pool_max_size=4)"
  Value error, pg_pool_min_size (20) exceeds pg_pool_max_size (4)
  ```
  (`_pool_bounds_are_orderable` is defined on `StoreSettings` and fires on `Settings`.)
- **Fix**: move the four single-section checks into their own sections as named validators
  (`_one_uvicorn_worker`, `_fleet_admission_fits_its_ceiling` → `ServiceSettings`;
  `_fleet_connections_fit_the_server` → `StoreSettings`; `_budgets_guard_something` →
  `ServiceSettings`), and the fan-out check into `ReportSettings`. `__init__.py` keeps the two
  genuinely cross-section rules. Behaviour-preserving: yes for any configuration that violates at
  most one rule; when two are violated simultaneously the *order* of the reported errors can change
  (pydantic surfaces all `after` validators' failures, but the ordering across bases is not the
  source order of the current single function). No test asserts multi-error ordering.

---

## The config package re-exports 19 section mixins; 18 have no consumer anywhere

- **Severity**: low
- **Location**: `src/chemclaw/core/config/__init__.py:55-108` (19 imports, existing solely to be
  re-exported, plus the `__all__` list)
- **Trigger**: adding or renaming a section — the name must be edited in three places (the module,
  the import, `__all__`) or `mypy --strict` fails on implicit re-export.
- **Consequence**: 19 import lines and a 25-entry `__all__` maintained for two consumers, and the
  comment justifying them is wrong. It says "every section mixin (a few are imported directly, e.g.
  `EvalSettings`)" — measured, **zero** section mixins are referenced from `src/` outside
  `core/config/`, and only `EvalSettings` is referenced through the package (two test files, both
  reading `model_fields[...].default`). `StoreSettings` is referenced by one test which imports it
  from `chemclaw.core.config.store` directly, bypassing the re-export it is listed for.
- **Evidence**: reference counts for all 19 mixin names outside `core/config/`:

  ```
  AgentSettings src=0 tests=0      LlmSettings src=0 tests=0
  BoSettings src=0 tests=0         MemorySettings src=0 tests=0
  CalculatorSettings src=0 tests=0 ObservabilitySettings src=0 tests=0
  ConnectorSettings src=0 tests=0  ReportSettings src=0 tests=0
  ElnSettings src=0 tests=0        RetrievalSettings src=0 tests=0
  EntraSettings src=0 tests=0      ServiceSettings src=0 tests=0
  EvalSettings src=0 tests=4       SourcesSettings src=0 tests=0
  FingerprintSettings src=0 tests=0 StoreSettings src=0 tests=2
  HpcSettings src=0 tests=0        TemporalSettings src=0 tests=0
  KgSettings src=0 tests=0
  ```
  (`tests/test_vector_store.py:386` imports `StoreSettings` from `...config.store`, not from the
  package.)
- **Fix**: reduce `__all__` to `["NOTE_INDEX_SOURCES", "SCHEMA_VECTOR_DIM", "Settings", "settings"]`
  and drop the 19 re-export imports; the composed class still inherits every mixin, so
  `settings.<anything>` is unchanged. Point `tests/test_retrieval_eval.py:19` and
  `tests/test_seed_corpus.py:17` at `chemclaw.core.config.evals`, which is where `StoreSettings`'
  test already goes. Behaviour-preserving: yes, apart from those two test imports.

---

## `CalculatorSettings`' headline claim — "None of them enters a cache key" — is false for `xtb_geometry_decimals`, which the field's own comment says three lines later

- **Severity**: medium
- **Location**: `src/chemclaw/core/config/calculators.py:33-37` (class docstring) vs `:57-62`
  (the `xtb_geometry_decimals` comment)
- **Trigger**: change `CHEMCLAW_XTB_GEOMETRY_DECIMALS` on this deployment.
- **Consequence**: every `Structure` this process builds is re-rounded, so `structure_id` changes,
  so every downstream relaxation/Hessian key misses and recomputes — the exact behaviour the class
  docstring tells a reader is now impossible ("changing anything here invalidates nothing and
  recomputes nothing"). The two claims are 20 lines apart in one file, and a reader who trusts the
  headline will treat this knob as free to tune.
- **Evidence**: `science/calc/models.py:105-113` reads `settings.xtb_geometry_decimals` inside
  `Structure._normalize_and_validate`, and that model's own docstring (`:75`) states
  "`structure_id` is half of every key those calculations are cached under". Measured:

  ```
  $ uv run python /tmp/probe_decimals.py
  xtb_geometry_decimals=4 -> st_de4d4fa766fabf0b  positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
  xtb_geometry_decimals=6 -> st_fb645aa897355b4c  positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 1.000005]]
  ```

  (Same two atoms, two different content addresses.) The field's own comment agrees with the
  measurement and contradicts the class docstring: "it is part of the structure id, so changing it
  re-addresses every structure and therefore recomputes."
- **Fix**: correct the class docstring to name the one exception — `xtb_geometry_decimals` reaches
  the key through `Structure`, because this repository still builds `Structure` objects (the
  thermochemistry refinement loop's displacement) even though the physics is remote. Docs-only, so
  trivially behaviour-preserving; the alternative (move the rounding to the server so the claim
  becomes true) is a cross-repo change and would silently re-address every cached structure. Note
  the same comment block references `xtb_embed_seed`, a field that does not exist in this
  repository at all (`grep` finds it only in that comment) — that one is in the correctness
  reviewer's report.

---

## Two separators for one documented category of list setting, and one delimited setting parsed at its call site

- **Severity**: medium
- **Location**: house rule at `src/chemclaw/core/config/__init__.py:27-33`; the eight pathsep
  properties (`agent.py:210,215,220,230,239`, `connectors.py:94,99`, `sources.py:106`); the comma
  parsers (`entra.py:106,111`, `sources.py:111`); the call-site parse at
  `src/chemclaw/api/middleware.py:205`
- **Trigger**: an operator reads the house rule, which puts `skills_dir`, `data_sources`,
  `data_sources_dir`, `connectors_dir`, `skills_enabled` and `entra_expensive_actions` in **one
  bullet** as "delimited string … the elements are bare keys … an admin sets these like `PATH`",
  and then sets `CHEMCLAW_DATA_SOURCES="graph:eln-json"` — or, symmetrically,
  `CHEMCLAW_CONNECTORS_ENABLED="calc,bo"`.
- **Consequence**: both are accepted silently and parse to a single nonsense name. The rule names
  `PATH` explicitly, and half the fields it lists are comma-separated.

  ```
  $ uv run python -c "..."
  data_source_list        = ['graph:eln-json']
  connectors_enabled_list = ['calc,bo']
  ```

  Separately, `service_cors_origins` is a delimited string with **no** derived property, parsed with
  the same idiom at its one call site — `api/middleware.py:205`:
  `origins = [o.strip() for o in settings.service_cors_origins.split(",") if o.strip()]` — against
  the same house rule's closing sentence, "Expose the parsed value through a derived `*_list`/`*_dirs`
  property and read that, never the raw string."
- **Evidence**: field-by-field separators — pathsep: `skills_dir`, `skills_enabled`, `profiles_dir`,
  `templates_dir`, `templates_enabled`, `connectors_dir`, `connectors_enabled`, `data_sources_dir`;
  comma: `data_sources`, `entra_expensive_actions`, `entra_privileged_roles`,
  `service_cors_origins`. Twelve fields, two idioms, one documented rule, and twelve hand-written
  comprehensions differing only in separator and loop variable name.
- **Fix**: two module-level helpers in the config package (`_pathsep_list(value)`,
  `_comma_list(value)`), each used by the properties; add a `service_cors_origin_list` property and
  read it from `api/middleware.py`; and split the house-rule bullet in two so the docstring states
  which separator each kind of key uses instead of implying one. Behaviour-preserving: yes — the
  helpers reproduce each comprehension exactly (note `data_source_list` and the entra sets `.strip()`
  each element while the pathsep ones do not; keep that difference in the two helpers rather than
  unifying it, or `CHEMCLAW_SKILLS_DIR="skills: /opt/x"` changes meaning).

---

## `MemorySettings` is a grab-bag whose name and docstring describe a fifth of its fields

- **Severity**: low
- **Location**: `src/chemclaw/core/config/memory.py:13-19` (class docstring) vs `:20-157`
- **Trigger**: an operator wants to enable retention, or raise the attachment upload limit, or cap
  `find_calculations`. The package docstring promises "a reader finds everything about one concern
  in one file" and names the sections after concerns; none of those three is "the memory layers".
- **Consequence**: the section holds at least seven unrelated concerns — playbook/campaign synthesis
  (7 fields, the documented one), the ungated observations tier (6), the retention sweep (10),
  mid-turn durable-job resume (2), the calibration ledger (2), calc-store query caps (3), standing
  digests (3), and uploaded-attachment parsing (5). 38 fields, and the docstring covers the first
  seven. The stated benefit of the D-156 split does not hold for the largest section, and it is the
  one section that would need splitting for the rule to be true.
- **Evidence**: read the field list at `memory.py:24-157` against the class docstring at `:13-19`
  ("these thresholds define what the semantic/episodic layers may claim … plus the synthesis jobs'
  timeout and Schedule cadence"). `attachment_max_concurrent_parses` (`:157`) is a thread-pool bound
  on multipart upload parsing at the front door; `retention_checkpoints_days` (`:94`) prunes
  LangGraph checkpoint tables; neither is a memory-layer threshold. Compare `ServiceSettings`, whose
  docstring enumerates its actual scope.
- **Fix**: split out `RetentionSettings` (the 10 `retention_*` fields, whose readers are
  `durable/retention.py`) and `AttachmentSettings` (the 5 `attachment_*` fields, read by
  `agent/attachments.py`), and add the remaining strays to the docstring or move them to the section
  that owns their reader (`calc_find_max_results` / `calc_artifact_max_chars` /
  `calc_outliers_max_results` belong beside the calculation store). Behaviour-preserving: yes —
  a mixin move changes no field name, no env name and no default, and the composed `Settings` is
  flat; add the new mixins to the `Settings` bases and to `ARCHITECTURE.md`'s row per `D-156`.

---

## `_BY_STRUCTURE` is built by an inline module-level loop that leaks three globals

- **Severity**: low
- **Location**: `src/chemclaw/core/reagents.py:244-246`
- **Trigger**: `import chemclaw.core.reagents`.
- **Consequence**: three module-level names survive the loop and become part of the module's
  namespace, holding whatever the last table entry happened to be. It is also the odd one out: the
  other three indexes (`_TABLE`, `_BY_COMPOUND`, `_DENSITY_BY_STRUCTURE`) are each built by a named
  function with a docstring, and this one is not — so the file reads as three factories and one
  stray loop.
- **Evidence**:

  ```
  $ uv run python -c "import chemclaw.core.reagents as r; print({k: r.__dict__[k] for k in ('_key','_smiles','_display')})"
  {'_key': 'tmsn3', '_smiles': 'C[Si](C)(C)N=[N+]=[N-]', '_display': 'trimethylsilyl azide'}
  ```
- **Fix**: wrap it in `_index_by_structure() -> dict[str, str]` matching its three siblings.
  Behaviour-preserving: yes.
