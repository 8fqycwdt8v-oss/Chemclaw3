# `tests/` — design and simplification

Reviewer lens: structure that costs more than it buys. Everything below was checked against the
code and, where a claim was cheap to settle, run. Findings are ordered by severity.

---

## The Temporal `thread`-timeout hook selects on an incidental attribute and misses two Temporal modules

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/tests/conftest.py:216` (`pytest_collection_modifyitems`), line 243:
  `if module is not None and hasattr(module, "start_env_or_skip")`
- **Trigger**: a test module that starts a real `WorkflowEnvironment` but imports
  `start_env_or_skip` **inside a function** rather than at module scope. Two modules do exactly
  that: `/home/user/Chemclaw3/tests/test_template_job_step.py:173` and `:407`, and
  `/home/user/Chemclaw3/tests/test_templates.py:474`.
- **Consequence**: `hasattr(module, "start_env_or_skip")` is `False` for those modules, so they
  never get `@pytest.mark.timeout(method="thread")` and stay on `pyproject.toml`'s
  `timeout_method = signal`. The hook's own docstring measured what that means: `signal` raises
  from a SIGALRM handler, the interpreter only runs handlers between bytecodes, and a test blocked
  inside `temporalio`'s PyO3 core never returns to run it — "a workflow submitted to a queue whose
  worker had not registered it hung `test_bo_knowledge.py` for **28 minutes** past a 600 s cap …
  no test name, no traceback". Both missed modules drive real workflows (`TemplateWorkflow`) against
  a real worker, which is the same hang shape. The hook is a control that silently does not cover
  two of the modules it exists for.
- **Evidence**: reproduced by collecting with a plugin that prints each module's closest `timeout`
  marker:

  ```
  $ PYTHONPATH=/tmp uv run pytest -p showmark --collect-only -q \
        tests/test_templates.py tests/test_template_job_step.py tests/test_qm_workflow.py
  MARK tests.test_templates          None
  MARK tests.test_template_job_step  None
  MARK tests.test_qm_workflow        {'method': 'thread'}
  ```

  and directly:

  ```
  tests.test_orchestrator:      hasattr(start_env_or_skip) = True
  tests.test_template_job_step: hasattr(start_env_or_skip) = False
  tests.test_templates:         hasattr(start_env_or_skip) = False
  tests.test_qm_workflow:       hasattr(start_env_or_skip) = True
  ```

  `test_orchestrator.py` only passes because its import sits inside
  `with workflow.unsafe.imports_passed_through():` at module scope — i.e. it passes by accident of
  indentation, not because the criterion is sound.
- **Fix**: stop inferring the property from a module namespace. Either (a) select by source text /
  AST — mark any item whose module file mentions `start_env_or_skip` or `WorkflowEnvironment` at any
  scope; or (b) better and simpler, delete the special case and set `timeout_method = "thread"`
  globally in `pyproject.toml`, since the one thing `signal` buys ("the session continues") is worth
  nothing for the failure mode the hook was written from. Option (b) is behaviour-preserving for
  every currently-marked module and strictly safer for the rest.

---

## Two shared modules define the same scripted-model double; one of the two definitions is redundant and one of its helpers is dead

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/tests/fakes.py:97` (`ScriptedModel`) and `:116` (`scripted`);
  `/home/user/Chemclaw3/tests/fakes_langgraph.py:24` (`ScriptedChatModel`) and `:102`
  (`scripted_call`); `/home/user/Chemclaw3/tests/test_langgraph_agent.py:97` (`_scripted`)
- **Trigger**: reading either module's docstring. `fakes.py` says it holds "the two test doubles that
  fourteen modules were each writing their own copy of"; `fakes_langgraph.py` says "a double that
  four test modules need is a double that must have one definition, or the suite ends up asserting
  against four subtly different models". Both claims cannot hold: they are two definitions of one
  double, with a verbatim-duplicated justifying paragraph ("Subclassed because `create_agent`'s model
  node calls `.bind_tools(...)` … measured, not assumed. Binding returns `self` here: the script
  already contains the tool call under test …") copied between them.
- **Consequence**:
  - `ScriptedChatModel` is a strict superset of `ScriptedModel` — same `bind_tools` override, plus a
    `_stream` that the other lacks and that `fakes_langgraph.py`'s own docstring documents as
    necessary under `astream`. A test written against `ScriptedModel` therefore silently cannot be
    driven through `graph_events` without hitting `ValueError: No generations found in stream`, and
    the author has to discover which of the two shared modules they were supposed to import from.
  - `fakes_langgraph.scripted_call` has **zero callers** anywhere in `tests/` or `src/`.
  - `test_langgraph_agent._scripted` is a byte-identical clone of `fakes.scripted` (AST-dump equal,
    675 chars) sitting in the module that already imports from `tests/fakes.py`, while
    `test_middleware_order.py` imports `scripted` from `tests/fakes.py`. So the same six-line
    fixture exists three times.
- **Evidence**: clone detection over the suite's ASTs reported
  `2 copies, size 675, names={'_scripted', 'scripted'} — tests/fakes.py:116, tests/test_langgraph_agent.py:97`.
  Reference counts (`grep -rn` over `tests/` and `src/`): `scripted_call` → 1 (its own definition).
  Substitution proof that `ScriptedModel` is redundant — I replaced its definition with
  `from tests.fakes_langgraph import ScriptedChatModel as ScriptedModel` and ran both consumers:

  ```
  $ uv run pytest tests/test_langgraph_agent.py tests/test_middleware_order.py -q
  ........................                                                 [100%]
  24 passed in 8.67s
  ```

  (baseline, unmodified, is the same 24 passed; the file was restored afterwards.)
- **Fix**: delete `ScriptedModel`, `scripted` and the `GenericFakeChatModel` import from
  `tests/fakes.py`; delete `scripted_call` from `tests/fakes_langgraph.py`; delete
  `test_langgraph_agent._scripted` and point its 9 call sites at
  `fakes_langgraph.ScriptedChatModel([{...}, "done"])`. Behaviour-preserving — demonstrated above.

---

## Dead scaffolding left by the agent-framework removal, including a test that is a byte-identical duplicate of the one above it

- **Severity**: medium
- **Location**:
  - `/home/user/Chemclaw3/tests/test_llm_provider.py:33` `_fake_openai_client_capture` — 0 callers;
    it `monkeypatch.setitem(sys.modules, "agent_framework.openai", …)` for a distribution that is not
    installed (`import agent_framework` → `ModuleNotFoundError`).
  - `/home/user/Chemclaw3/tests/test_llm_provider.py:25` `test_anthropic_path_preflights_missing_key`
    vs `:94` `test_anthropic_model_path_preflights_missing_key` — identical bodies.
  - `/home/user/Chemclaw3/tests/test_runner.py:216` `_PlanClearingAgent` — a 20-line `ScriptedTurn`
    subclass, 0 references, docstring cites "MAF's own todo instructions".
  - `/home/user/Chemclaw3/tests/test_dialogue.py:101` `_Function` + `:108` `_Context` — 0 external
    references; docstring: "The slice of `FunctionInvocationContext` the dry-run gate touches".
    `FunctionInvocationContext` no longer exists; the live `_call` uses `tool_request` /
    `run_middleware`.
  - `/home/user/Chemclaw3/tests/test_connector_jobs.py:106` `_DryRunContext` — same class, same dead
    `FunctionInvocationContext` premise, 0 references.
  - `/home/user/Chemclaw3/tests/test_plan_gate.py:239` `_proceed` — 0 references; docstring:
    "Normalize a `should_continue` result the way MAF's loop does".
- **Trigger**: any reader trying to work out which of the two `..._preflights_missing_key` tests
  covers which engine, or what `_DryRunContext` is a slice *of*.
- **Consequence**: the two preflight tests call `provider.build_chat_model()` with the same settings
  and the same deleted env var, so the second proves nothing the first did not — while the comment
  block between them (`test_llm_provider.py:45-51`) tells the reader that "the MAF tests above …
  must" fake clients through `sys.modules`, naming tests that no longer exist and a fake that is
  never invoked. Roughly 70 lines of test code describe a framework the repo removed, and a reader
  cannot tell the dead half from the live half without grepping.
- **Evidence**: the reference counts above are from a whole-suite AST + token scan
  (`/tmp/dead2.py`), each confirmed with `grep -rn '\b<name>\b' tests/ src/ --include=*.py`; every
  one returned exactly its own definition line. `uv run python -c "import agent_framework"` →
  `ModuleNotFoundError: No module named 'agent_framework'`.
- **Fix**: delete all six symbols and the duplicate test; delete the `# --- the LangGraph half of
  the seam` comment's reference to "the MAF tests above". Behaviour-preserving (nothing calls them,
  and the surviving preflight test asserts the identical thing).

---

## Fourteen hand-rolled walks of `src/**/*.py`, with sixteen names for the repo root and a verbatim-duplicated AST scope visitor

- **Severity**: medium
- **Location**: the walk sites — `tests/test_layering.py:203`, `tests/test_third_party_layering.py:424`,
  `tests/test_db.py:196`, `tests/test_docstring_paths.py:155`, `tests/test_scratchpad.py:128`,
  `tests/test_authz.py:295`, `tests/test_metric_declarations.py:90` and `:122`,
  `tests/test_run_jitter.py:111`, `tests/test_durable_heartbeat.py:250`,
  `tests/test_database_privileges.py:173`, `tests/test_degraded.py:79`, `tests/test_config.py:692`,
  `tests/test_calc_remote.py:395`. The duplicated visitor — `tests/test_layering.py:136-201`
  (`_ImportVisitor`) vs `tests/test_third_party_layering.py:336-421` (`_Visitor`).
- **Trigger**: adding a directory under `src/chemclaw/` that a structural gate must skip (generated
  code, a vendored file), or changing how "which scope is this import written at" is decided.
- **Consequence**:
  - Fourteen edit sites instead of one. There is no shared `tests/tree.py`, even though the suite
    already has `tests/pg.py`, `tests/fakes.py`, `tests/fakes_langgraph.py`, `tests/fakes_turn.py`,
    `tests/middleware.py`, `tests/signals.py`, `tests/surface.py` for smaller shared concerns.
  - `_descend`, `visit_FunctionDef`, `visit_AsyncFunctionDef`, `visit_If` and the
    `"type_checking" / "function" / "module"` scope expression are duplicated **verbatim** between
    the two layering modules; the AST clone detector reports `visit_If` as identical at 1983
    characters and `visit_Import` at 549. A fix to the scope rule in one leaves the other on the old
    behaviour, silently — and both files' docstrings claim to own "the same scope rules".
  - The root path is rederived in 29 modules under 16 different constant names (`_REPO_ROOT` ×8,
    `_ROOT` ×6, `_SRC` ×3, `_REPO`, `_SRC_ROOT`, `_KNOWLEDGE`, `_DECISIONS`, …), some as
    `parents[1]`, some as `parent.parent`.
  - It is measurably slow: the tree is 341 files / 3.8 MB, one full parse pass is 247 ms, and the two
    module-level walks alone cost **1.06 s at import time** (`test_layering` 589 ms,
    `test_third_party_layering` 472 ms) — paid on every `--collect-only` and every `-k` run.
- **Evidence**:

  ```
  src py files: 341  bytes: 3814694
  one full parse pass: 247 ms
  tests.test_layering              589 ms import (module-level src walk)
  tests.test_third_party_layering  472 ms import (module-level src walk)
  ```

  Clone detector output: `--- 2 copies, size 1983, names={'visit_If'} → tests/test_layering.py:162,
  tests/test_third_party_layering.py:357`.
- **Fix**: add `tests/tree.py` holding `REPO_ROOT`, `SRC_ROOT`, a `@cache`d
  `parsed_sources() -> list[tuple[Path, ast.Module]]`, and one `ScopedImportVisitor` parameterised by
  a `keep(target) -> bool` predicate and a `record(...)` hook — the only two things the layering
  visitors actually differ in. Point all fourteen sites at it. Behaviour-preserving; the payoff is
  one edit site for the scope rule and ~4-6 s of parsing removed from every run.

---

## `test_live_probes.py` and `test_m12_probes.py` are the same subject split by plan phase, and duplicate four helpers and three corpus gates

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/tests/test_live_probes.py` (569 lines) and
  `/home/user/Chemclaw3/tests/test_m12_probes.py` (688 lines)
- **Trigger**: adding a rule to the probe-corpus gate — e.g. "no probe may name a retired persona".
  A contributor adds it to one file and the other corpus is ungated.
- **Consequence**: both files test `chemclaw.evals.live` driven over `httpx.MockTransport`, and both
  gate a probe directory. Duplicated:
  - `_probe(**overrides)` — identical body, only the default `id` differs
    (`test_live_probes.py:45` / `test_m12_probes.py:69`);
  - `_sse(*events)` — byte-identical (`:59` / `:83`);
  - the `handler(request)` closure that answers `/sessions` then streams `_sse(...)` — flagged
    identical at 737 chars by the clone detector (`:67` / `:91`);
  - `_run` / `_run_one` — same `AsyncClient(transport=…, base_url="http://front-door")` body;
  - `test_probe_files_carry_nothing_but_probes` (`:331`) vs
    `test_every_m12_probe_file_carries_nothing_but_probes` (`:634`) — identical but for the directory;
  - `test_every_expected_tool_in_the_shipped_corpus_exists_on_the_agent_surface` (`:308`) vs
    `test_every_expected_tool_in_the_m12_corpus_exists_on_the_agent_surface` (`:642`) — same, with
    the set comprehension reflowed;
  - the duplicate-id gate, in `load_probes` for one and as an explicit test for the other.
- **Evidence**: clone detector — `2 copies, size 554, names={'_sse'}` and
  `2 copies, size 737, names={'handler'}` across exactly these two files; the corpus tests are
  reproduced side by side above from `sed -n 300,340p` / `sed -n 626,660p`.
- **Fix**: move `_probe`, `_sse`, `_transport`, `_run` into one `tests/probes.py`; parametrize the
  three corpus gates over `[PROBE_DIR, M12_DIR]` in a single module. The two files' *distinct*
  content (the plan-gate and degradation scorers) then stands alone and is readable. Behaviour-preserving.

---

## Four copies of the "uvicorn on a background thread" server and five of the recording audit sink, in a suite whose shared-fakes modules exist to prevent exactly this

- **Severity**: medium
- **Location**:
  - `_Server`: `tests/test_connector_transport.py:79`, `tests/test_langgraph_connectors.py:45`,
    `tests/test_connector_safety_rubric.py:71`, plus the same start/stop loop inlined into
    `tests/test_verifier.py:~805-838` (`_FakeOpenAiEndpoint`).
  - `_RecordingSink`: `tests/test_audit.py:92`, `tests/test_connector_safety_rubric.py:61`,
    `tests/test_tool_authz.py:711`, `tests/test_langgraph_agent.py:209` (`_CollectingSink`),
    `tests/test_middleware_order.py:186` (`_Recording`), plus duck-typed one-offs at
    `tests/test_template_job_step.py:217`, `tests/test_tool_invocation.py:102`,
    `tests/test_job_record.py:193`, `tests/test_templates.py:617`, `tests/test_template_agent_step.py:46`.
  - BO family: `_problem()` identical in `tests/test_bo_predict.py:40`,
    `tests/test_bo_provenance.py:51`, `tests/test_bo_tools.py:36`; the `store` fixture identical in
    `tests/test_bo_campaign_record.py:71` and `tests/test_bo_provenance.py:68`.
  - Free-port grabbing: `tests/conftest.py:44` owns it, but `tests/test_temporal_client.py:29` and
    `tests/test_tool_authz.py:578` each open their own socket instead.
- **Trigger**: the 10 s `self._thread.join(timeout=10)` or the 200×0.05 s start poll turning out to
  be too tight on a loaded machine — four edits. Or `AuditSink` gaining a second method — five
  edits, and the duck-typed ones will not even fail loudly (it is a `Protocol`, not an ABC:
  `src/chemclaw/agent/audit.py:149`).
- **Consequence**: `tests/fakes.py`'s own docstring states the rule this violates — "a shared double
  earns its place when the copies have already drifted or are already boilerplate at the call site"
  — and `tests/fakes_langgraph.py`'s states "Extracted at the third caller … which is the repo's Rule
  of Three". Four and five copies are past three. The `_Server` copies have already begun to drift:
  `test_connector_safety_rubric.py` inlines the `uvicorn.Config` while the other two keep it as
  `self._config`, and the start-poll comment survives in two of the three.
- **Evidence**: AST clone detector reports `3 copies, size 842, names={'__enter__'}` and
  `4 copies, size 376, names={'__exit__'}` and `2 copies, size 1082, names={'__init__'}` across
  exactly these files; `3 copies, size 852, names={'_problem'}` and
  `2 copies, size 628, names={'store'}` across the BO files.
- **Fix**: move `_Server` into `tests/fakes.py` (or a `tests/servers.py`) beside `_free_port` — which
  should also stop being underscore-private, since six modules import it by that name; move
  `RecordingAuditSink` into `tests/fakes.py`; add `tests/bo.py` for `_problem` / `store` / `_run`.
  All behaviour-preserving.

---

## `tests/test_service.py` is an undeclared shared-fixture library, imported privately (and lazily) by four other modules

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/tests/test_service.py` (1530 lines) defines `_FakeAgent:67`,
  `_no_connectors:79`, `_app:88`, `_client:101`, `_FakeOwnerStore:555`. Importers:
  `tests/test_capability_degradation.py:30` (module scope),
  `tests/test_session_profile_survives_eviction.py:30` (module scope),
  `tests/test_hot_path_caching.py:84` and `:112` (inside functions),
  `tests/test_turn_observability.py:246` (inside a function).
- **Trigger**: renaming or reshaping any of those five private helpers, or running
  `tests/test_capability_degradation.py` alone — which imports and therefore *collects nothing from*
  but fully executes the module body of a 1530-line test file.
- **Consequence**: five doubles that four other modules depend on live in a module named after the
  thing under test, are `_`-prefixed to say "private", and are pulled in lazily in two places
  (function-scope import, the shape usually used to dodge an import cycle). This is precisely the
  arrangement `tests/fakes.py` was created to end: "a double that four test modules need is a double
  that must have one definition". `tests/README.md` lists four shared-double modules and none of them
  is this one, so a new contributor has no way to know the front-door doubles live here.
- **Evidence**: `grep -rn "from tests.test_" tests/ --include=*.py` returns exactly the five sites
  above and nothing else.
- **Fix**: move `_FakeAgent`, `_no_connectors`, `_app`, `_client`, `_FakeOwnerStore` into
  `tests/fakes.py` (they are ~60 lines total), drop the underscore, and make all five imports
  module-scope. Behaviour-preserving.

---

## `tests/README.md` is a hand-maintained index of the suite's own structure, and it has drifted in both of its lists

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/tests/README.md:11-18` ("Four modules hold shared **doubles**")
  and `:25-42` (the structural-gate table)
- **Trigger**: looking for the shared helper that does what you need before writing your own.
- **Consequence**: this is the same drift shape `tests/test_layering.py`'s docstring says it fixed
  for `src/` — "Those lists drifted from disk … drift in an *allow-list* is invisible" — reproduced
  inside `tests/` itself, and nothing reads `tests/README.md` to check it (`grep -rn "tests/README"`
  over `tests/`, `src/` and `Makefile` returns one *mention* in a docstring and no validator).
  - **Doubles**: there are 13 non-test modules in `tests/` (`calc_server_fake.py`, `conftest.py`,
    `document_fixtures.py`, `fakes.py`, `fakes_langgraph.py`, `fakes_turn.py`, `legacy_rows.py`,
    `middleware.py`, `pg.py`, `recorded_tool_results.py`, `signals.py`, `surface.py`,
    `temporal_env.py`). Seven are named in the README; **six are not** — including
    `calc_server_fake.py`, the 432-line stand-in for the whole `calc` MCP server, and `middleware.py`
    / `signals.py` / `surface.py`, which are the entry points for testing a middleware, a turn signal
    and a profile respectively.
  - **Structural table**: it carries an obligation ("adding a module to this table means giving it
    [an emptiness pin]"), and nine modules that AST-walk `src/**/*.py` as structural gates are absent
    from it: `test_authz.py`, `test_calc_remote.py`, `test_config.py`, `test_db.py`,
    `test_degraded.py`, `test_durable_heartbeat.py`, `test_durable_tools.py`, `test_run_jitter.py`,
    `test_scratchpad.py`. Whatever emptiness discipline the table imposes, those nine were never
    asked for it.
- **Evidence**: set difference between `grep -l 'rglob("\*\.py")' tests/test_*.py` and the module
  names appearing in `tests/README.md`, printed above; module inventory from `ls tests/*.py`.
- **Fix**: either derive both lists (a test that asserts every non-`test_` module in `tests/` appears
  in the README, the same shape as `tests/test_repo_map.py` for `src/`), or delete the two lists and
  keep only the prose. A hand-maintained index with no validator is the thing this repo tests
  against everywhere else.

---

## Two tests are named `testall_…`, so `-k "test_"` deselects them

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/tests/test_memory_jobs.py:24`
  `testall_reactions_honors_data_sources_config` and `:35`
  `testall_reactions_empty_when_no_ingest_source_active`
- **Trigger**: a missing underscore. pytest's default `python_functions = "test*"` still collects
  them, so they run — but any selection expression or grep spelled `test_` misses them.
- **Consequence**: the two tests that pin DUP-1 (`all_reactions` honouring `settings.data_sources`)
  are invisible to the idiom the repo's own `tests/README.md` documents for running a subset
  (`pytest -k "substring"`), and to a `grep -n "^def test_"` inventory of the file.
- **Evidence**:

  ```
  $ uv run pytest tests/test_memory_jobs.py -q --collect-only
  tests/test_memory_jobs.py::testall_reactions_honors_data_sources_config
  tests/test_memory_jobs.py::testall_reactions_empty_when_no_ingest_source_active
  tests/test_memory_jobs.py::test_a_corpus_read_that_skipped_an_entry_reports_itself_incomplete
  tests/test_memory_jobs.py::test_background_worker_registers_memory_fan_out
  4 tests collected

  $ uv run pytest tests/test_memory_jobs.py -q --collect-only -k "test_"
  tests/test_memory_jobs.py::test_a_corpus_read_that_skipped_an_entry_reports_itself_incomplete
  tests/test_memory_jobs.py::test_background_worker_registers_memory_fan_out
  ```
- **Fix**: rename to `test_all_reactions_…`. Behaviour-preserving.

---

## What I checked and did not find

- **`tests/calc_server_fake.py` has not drifted from the tool surface it stands in for.** Every tool
  name the fake dispatches (`_KEYED` ∪ `_UNKEYED` ∪ `calculation_key`) is exactly the set of literals
  the in-repo client calls (`grep -hoE` over `src/chemclaw/connectors/calc/` and
  `src/chemclaw/science/` → `calculation_key`, `combine_structures`,
  `compute_electronic_properties`, `compute_hessian`, `compute_xtb_energy`, `embed_structure`,
  `predict_developability_profile`, `predict_pka`, `predict_site_reactivity`, `predict_solubility`,
  `relax_structure`, `scan_point`, `search_binding_modes`, `search_conformer_ensemble`), plus
  `predict_logd`, which the fake deliberately raises on because the client composes it locally.
- **`_Recording.prompts: list[str] = []` in `tests/test_langgraph_agent.py:87` is not the shared
  mutable class attribute it looks like.** `BaseChatModel` is a pydantic model, so that declaration
  becomes a *field* with a per-instance deep-copied default. Verified by instantiating two and
  mutating one: `same object: False`. Not a finding.
- **Every shared helper module in `tests/` has at least two importers** (`surface` 4, `middleware` 6,
  `signals` 4, `legacy_rows` 4, `document_fixtures` 2, `temporal_env` 12, `recorded_tool_results` 2,
  `fakes` 8, `fakes_langgraph` 10, `fakes_turn` 11, `pg` 29), so none is a single-caller abstraction
  to inline.
- **The classes `TestTheAntiFeedbackRule` / `TestTheCorpusMiner` / `TestTheInteractionMiner` in
  `tests/test_observations.py` are not dead** — pytest's default `python_classes = "Test*"` collects
  them.
