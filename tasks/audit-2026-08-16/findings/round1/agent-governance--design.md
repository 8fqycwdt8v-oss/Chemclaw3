# Round 1 — agent governance slice, design & simplification lens

Slice: `src/chemclaw/agent/{authz,tool_authz,plan_gate,skill_access,skill_backend,tool_invocation,audit,audit_store,turn_flags}.py`

All measurements below were run with `uv run` against the live venv in this repo.

---

## Audit `detail` records the transport repr that the neighbouring function exists to avoid

- **Severity**: medium
- **Location**: `src/chemclaw/agent/audit.py:282` and `:288` (`make_audit_middleware.audit_tool_calls`), against `src/chemclaw/agent/tool_authz.py:141` (`returned_failure_detail`)
- **Trigger**: any connector (MCP) tool call. `langchain_mcp_adapters._handle_mcp_tool_error` returns `list[ToolMessageContentBlock]`, so a failed connector call *always* comes back as `ToolMessage(content=[{"type":"text","text": …}], status="error")`. `make_audit_middleware` then writes `_truncate(failed.content)` into `AuditEvent.detail`, and `_truncate` is `repr(value)[:settings.agent_audit_max_arg_chars]`.
- **Consequence**: the same decision — "how do we render a `ToolMessage` payload for a human to read" — is implemented twice with different answers, and the copy in `audit.py` is the worse one. The chemist's transcript gets the server's sentence; the `audit_events.detail` column, which the module docstring calls the record of the call's *effect*, gets a Python repr of the transport envelope truncated at a **different** limit (200, config) from the transcript's (300, hardcoded). `returned_failure_detail`'s docstring states the rule explicitly — *"`message.text` rather than `message.content`: MCP content arrives as a list of content blocks, so a chemist reading `content` would get `[{'type': 'text', 'text': …}]` — a repr of the transport where the explanation should be"* — and the audit path is exactly that case. The `ok` path has the same shape (`recorded.result = getattr(result, "content", result)`), so a *successful* connector call is also recorded as a block-list repr.
- **Evidence**: measured, not argued.

  ```
  agent_audit_max_arg_chars = 200

  TRANSCRIPT (tool_authz.returned_failure_detail, 300 chars, .text):
  "xTB refused the input: the molecule 'CC(=O)Oc1ccccc1C(=O)O' was parsed but the requested
   GFN2 optimisation did not converge after 500 cycles; the last gradient norm was 4.2e-2
   Eh/a0, which is above the 1e-4 threshold. Re-run with a looser convergence criterion or a
   different starting geometry."

  AUDIT audit_events.detail (audit._truncate(content), 200 chars, repr):
  '[{\'type\': \'text\', \'text\': "xTB refused the input: the molecule \'CC(=O)Oc1ccccc1C(=O)O\'
    was parsed but the requested GFN2 optimisation did not converge after 500 cycles; the last
    gradient norm was 4.2e…'
  ```

  26 characters of the 200-char budget are spent on `[{'type': 'text', 'text': ` before any explanation starts. The `ok` case renders identically: `"[{'type': 'text', 'text': 'pKa = 4.2 (predicted)'}]"` where the model was handed `'pKa = 4.2 (predicted)'`.

  Also confirmed against the installed adapter that the list shape is the production shape and not a synthetic one — `_handle_mcp_tool_error` is documented to return "the content blocks carried by an `_MCPToolExecutionError` … so the caller produces a `ToolMessage` with `status="error"`".
- **Fix**: move the one rendering rule into `audit.py` (the dependency already points that way — `tool_authz` imports `returned_failure` from `audit`, never the reverse) as e.g. `def message_detail(message: ToolMessage) -> str: return message.text`, have `tool_authz.returned_failure_detail` call it, and have `audit_tool_calls` use `result.text` rather than `result.content` on both the `ok` and the returned-error paths. While doing so, replace `tool_authz._FAILURE_CHARS = 300` with the existing `settings.agent_audit_max_arg_chars` (or a sibling setting) so the two bounds on the same text stop disagreeing — a hardcoded threshold is also against this repo's own "config, never magic numbers" rule, and the neighbouring bound is already a setting. Behaviour-changing by design (the recorded string improves); everything else preserved.

---

## The privileged-role check is cloned in both gates, and `_has_required_role` has two opposite contracts

- **Severity**: medium
- **Location**: `src/chemclaw/agent/authz.py:345-354` (`authorize_tool`) and `:376-385` (`authorize_trigger`); the shared predicate at `:283-292` (`_has_required_role`)
- **Trigger**: reading the module docstring at `:19-20` — *"Both defer the same role-membership predicate to `_has_required_role`, so the two gates can never drift in how 'does this user hold an allowed role?' is decided (DRY)"* — and then writing a third privileged gate the way that sentence tells you to.
- **Consequence**: the shared predicate is only half of the actual rule. `_has_required_role(required)` returns `True` for an empty `required` ("no specific role needed"), which is correct for an operator's `tool_role_gates` entry and **fail-open** for a privileged gate. Both privileged call sites therefore hand-write the missing half:

  ```python
  privileged = settings.entra_privileged_role_set
  if not privileged or not _has_required_role(privileged):
      raise AuthorizationError(...)
  ```

  verbatim in two places, each preceded by a 3-line and an 8-line comment restating the same fail-closed argument. This is the exact drift the docstring claims is impossible: the predicate that is shared is not the predicate that decides, and the one that decides is copied. A fourth caller following the docstring and writing `if not _has_required_role(settings.entra_privileged_role_set)` gets a gate that is open on the shipped chart shape the comment itself describes (`entra_required=true` with both role settings empty).
- **Evidence**: the two clone sites above, plus `authz.py:377-383`'s own comment conceding the rule is stated twice: *"the same rule the built-in write gate in `authorize_tool` states, and now reachable for the same reason"*.
- **Fix**: add one predicate beside `_has_required_role` and call it from both sites:

  ```python
  def _holds_privileged_role() -> bool:
      """Whether the turn's user holds a privileged role. Empty set = fail closed, never open."""
      privileged = settings.entra_privileged_role_set
      return bool(privileged) and _has_required_role(privileged)
  ```

  Then `authorize_tool` and `authorize_trigger` each become `if not _holds_privileged_role(): raise …`, and the eleven lines of duplicated comment collapse into that docstring. Strictly behaviour-preserving (identical truth table). Optionally tighten `_has_required_role`'s docstring so the empty-set convention is described as the *operator-gate* convention rather than a general one.

---

## `_SKILL_READ_LIMIT` is dead, and its comment claims the opposite of what happens

- **Severity**: medium
- **Location**: `src/chemclaw/agent/skill_backend.py:200-204` (`_SKILL_READ_LIMIT`), against `NarrowedSkillsBackend.read` at `:103`
- **Trigger**: the model calls `read_file("/chemclaw/computational-evidence/SKILL.md")` without passing `limit`, as it will whenever it does not copy the prompt's example exactly.
- **Consequence**: the skill body is truncated at **100 lines**, which is precisely what the constant's comment says it prevents: *"The default lives here rather than in the model's hands so a skill is not silently truncated when the model forgets."* Nothing reads the constant. `read_file`'s schema default is deepagents' `DEFAULT_READ_LIMIT = 100`, which is passed explicitly into the backend, so `NarrowedSkillsBackend.read`'s own `limit: int = 2000` default never applies either. 9 of the 29 shipped `SKILL.md` files exceed 100 lines (longest: `connectors/bo/skills/experiment-design/SKILL.md` at 302), so a third of the skills tree is silently truncatable while a module constant asserts it is not.
- **Evidence**:

  ```
  $ rg -n "_SKILL_READ_LIMIT" src tests
  src/chemclaw/agent/skill_backend.py:204:_SKILL_READ_LIMIT = 1000        # sole occurrence

  $ uv run python -c "from deepagents.middleware.filesystem import DEFAULT_READ_LIMIT; print(DEFAULT_READ_LIMIT)"
  100

  $ find skills src/chemclaw/connectors -name SKILL.md | wc -l          -> 29
  $ ... -exec wc -l {} + | awk '$1>100 && $2!="total"{n++} END{print n}' -> 9
  ```

  The upstream prompt does ask the model to pass `limit=1000` (verified by printing `SKILLS_SYSTEM_PROMPT`), which is exactly "in the model's hands" — the state the comment says was deliberately avoided.
- **Fix**: `read` is already overridden in this class, so enforce it there in one line — `return super().read(file_path, offset, max(limit, _SKILL_READ_LIMIT))` — which makes the comment true. If that is not wanted, delete the constant; a config value nothing reads, defended by a paragraph, is worse than no constant. (Note `SKILL_READ_TOOL` next to it is also production-unreferenced, but it is genuinely load-bearing as the pin `tests/test_skill_backend.py` asserts against the upstream prompt, so it should stay.)

---

## `STATE_CHANGING_TOOLS`' registry comment is false, and the partition test is one-directional as a result

- **Severity**: medium
- **Location**: `src/chemclaw/agent/authz.py:127-130`; the test it justifies is `tests/test_authz.py:202-213`
- **Trigger**: build the surface and compare the in-process classification sets against the live registry.
- **Consequence**: the comment states *"`STATE_CHANGING_TOOLS` also names tools that are not in-process at all (`compute_dft_energy` is a connector job, `index_*` are MCP tools behind an `allowed_tools` boundary) … Those are correct entries and correctly absent from the registry."* Measured, **every** name in `STATE_CHANGING_TOOLS`, `DEFAULT_WRITE_TOOL_GATES` and `READ_ONLY_TOOLS` is on the registry today — connector jobs are registered through the same `register_tool` (`connectors/jobs.py:12-17`) and the `index_*` entries were deleted. The comment is the stated reason the partition test only checks `advertised - classified` and never the reverse, so a classification entry that names nothing is invisible — which is the exact failure the file describes as having already happened once (`authz.py:76-80`: *"A deny-list entry for a name nothing serves reads as a control and is not one, and nothing validates these names against the live tool surface"*).
- **Evidence**:

  ```
  $ uv run python -c "...; surface(None); adv=set(registered_tool_names()); ..."
  advertised: 28
  DEFAULT_WRITE_TOOL_GATES not on surface: []
  STATE_CHANGING_TOOLS    not on surface: []
  READ_ONLY_TOOLS         not on surface: []
  side_effecting not on surface: ['compute_electronic_properties', 'compute_thermochemistry',
    'compute_xtb_energy', 'optimize_geometry', 'predict_developability_profile', 'predict_logd',
    'predict_outcome', 'predict_pka', 'predict_site_reactivity', 'predict_solubility',
    'report_measurement', 'suggest_next_experiment']
  ```

  The last line is the *connector-declared* half (endpoint `state_changing` tools, which are MCP tools and legitimately not in the registry), so the reverse check is impossible for `side_effecting_tools()` — and entirely possible for the three hand-kept sets, which is where a stale entry can hide.
- **Fix**: delete the stale sentence and add the other direction to `tests/test_authz.py`, over the hand-kept sets only:

  ```python
  assert (STATE_CHANGING_TOOLS | READ_ONLY_TOOLS) - advertised == set(), (
      "these names are classified but nothing serves them; a gate entry for a tool that does "
      "not exist reads as a control and is not one"
  )
  ```

  Behaviour-preserving (test-only), and it turns the one failure mode the file already documents from invisible into loud.

---

## `agent/authz.py` and the connector/template registries import each other; the cycle is hidden in function bodies

- **Severity**: low
- **Location**: `src/chemclaw/agent/authz.py:182-183` (inside `side_effecting_tools`) and `:270` (inside `expensive_actions`), against `src/chemclaw/templates/registry.py:25` and `src/chemclaw/connectors/jobs.py:37`
- **Trigger**: static reading — `templates/registry.py` does `from chemclaw.agent.authz import require_actor` at module scope, `connectors/jobs.py` does `from chemclaw.agent.authz import authorize_trigger, require_actor` at module scope, and `authz` imports both registries back from inside two function bodies.
- **Consequence**: the module the codebase calls "the one home for authorization" cannot state its own dependencies. Two of its five public functions can only be typed and imported at call time, and the cost is paid in comments rather than structure: both function-local imports carry an explanatory paragraph, and the paragraphs are wrong about the shape of the cycle — they say *"the connector and template registries reach the agent builder, which reaches this module"*, whereas `templates/registry.py` and `connectors/jobs.py` import `chemclaw.agent.authz` **directly**, with no builder in between. A deferred import also moves failure: `enabled()` raises `ConnectorError` for an unknown name in `connectors_enabled`, so a misconfigured deployment discovers it on the first gated tool call rather than at import.
- **Evidence**:

  ```
  $ rg -n "chemclaw.agent" src/chemclaw/connectors/*.py src/chemclaw/templates/registry.py
  src/chemclaw/connectors/jobs.py:37:from chemclaw.agent.authz import authorize_trigger, require_actor
  src/chemclaw/templates/registry.py:25:from chemclaw.agent.authz import require_actor
  ```

  (There is no runtime hazard from the deferral itself — `side_effecting_call` measured at ~0.01 ms/call over 1000 iterations, since both registries are `@cache`d. This is a structure finding, not a performance one.)
- **Fix**: `authz` holds two different things — *policy* (`authorize_tool`, `authorize_trigger`, `require_actor`, `_has_required_role`, the hand-kept sets) and *assembly over the live surface* (`side_effecting_tools`, `expensive_actions`). Only the second needs the registries. Move those two functions to a module that already sits above both — the natural home is beside `chemclaw.core.tool_registry`, or a new `chemclaw/agent/tool_classification.py` that imports `authz`'s sets, `connectors.registry` and `templates.registry` at module scope. Both are then ordinary top-level imports, the cycle disappears, and `authz` becomes importable by anything. Behaviour-preserving; it is a move plus re-export, with callers (`tool_authz`, `plan_gate`, `scratchpad`, `durable/template_activities`, `cli/live_probes`, `cli/validate_templates`, `evals/live`) updated to the new path.

---

## `_recording`'s four exit paths repeat the same five lines, for a second engine that no longer exists

- **Severity**: low
- **Location**: `src/chemclaw/agent/audit.py:311-444` (`_recording`), with `_Recorded` at `:294`
- **Trigger**: reading it. `elapsed_ms = (time.perf_counter() - start) * 1000.0` followed by `_observe_tool_latency(elapsed_ms)` appears three times; four near-identical blocks (`cancelled`, raised `error`, returned `error`, `ok`) each compute the elapsed time, log at WARNING/INFO with the same six-field format string, and emit an event.
- **Consequence**: a 130-line generator with one mutable out-parameter object (`_Recorded`), four exit paths and no single place where "an audit row is finished" happens — so the `cancelled` outcome had to be added as a fourth copy of the block rather than an argument, and a fifth outcome would be a fifth copy. The stated justification for the shape is dead: `_recording`'s docstring says *"both engines' middlewares are wrappers"* and *"A second copy of it for the second engine would be the one duplication this system cannot afford"*, but the Microsoft Agent Framework was removed (M13) and `_recording` now has exactly **one** caller, `make_audit_middleware`, forty lines above it in the same file. The context-manager-plus-out-parameter inversion exists purely to serve a second caller that does not exist.
- **Evidence**: `rg -n "_recording|_Recorded" src tests` → only `src/chemclaw/agent/audit.py`, plus two prose mentions in `tool_invocation.py` and one in `tests/test_tool_invocation.py`. No importer.
- **Fix**: two steps, both behaviour-preserving. (a) Extract the repeated tail into one local closure — `def finish(outcome: str, detail: str, *, level: int, shielded: bool = False) -> Awaitable[None]` — computing elapsed once and doing the observe/log/emit; the four branches become one line each. (b) Once there is one caller, collapse `_recording` + `_Recorded` into the middleware as a plain `try/except/else` around `await handler(request)`, deleting the generator and the out-parameter object. Keep the "framework-free" property by keeping the `ToolMessage` test in `returned_failure` where it already is.

---

## Nine single-caller indirections kept by a two-engine argument that is dead

- **Severity**: low
- **Location**: `src/chemclaw/agent/tool_authz.py:85-209` (`dry_run_refusal`, `undeclared_write_refusal`, `denial_result`, `domain_error_result`, `failure_detail`, `returned_failure_detail`, `answered_failure`, `unexpected_error_result`); `src/chemclaw/agent/plan_gate.py:115-122` (`gated_call`) and `:140-160` (`autonomy_for`, `PLAN_ONLY`)
- **Trigger**: follow any refusal message from the middleware that produces it to the f-string that words it.
- **Consequence**: five of the eight `tool_authz` functions are a single-expression f-string with exactly one production caller located 100-150 lines below in the same file (`denial_result`, `domain_error_result`, `failure_detail`, `unexpected_error_result`, and `returned_failure_detail` — the last has a second caller in `tool_invocation.py`, so it earns its place). `plan_gate.gated_call` is a pure alias for `authz.side_effecting_call` with one caller in the same module. The section header at `tool_authz.py:85-92` states the reason: *"it was written while two engines had to agree on it … One engine is left and the separation still earns its place — the plumbing is the part that changes when a library does."* The remaining argument is about `wrap_tool_call` churn, and it does not apply to `f"Refused: {exc}"`, which contains no plumbing to isolate. Two neighbouring comments are also now counterfactual: `plan_gate.py:126-129` justifies `PLAN_ONLY` as *"A constant because two decisions compare against it"* when the only comparison in the tree is `gate_applies`, and `autonomy_for`'s docstring justifies itself as the one resolver for three readers when `rg` finds one production caller (`gate_applies`, same module).
- **Evidence**:

  ```
  denial_result           -> tool_authz.py (surface_authorization_denials) + tests
  domain_error_result     -> tool_authz.py (surface_domain_errors) only
  failure_detail          -> tool_authz.py (announce_tool_failures) only
  unexpected_error_result -> tool_authz.py (surface_domain_errors) only
  undeclared_write_refusal-> tool_authz.py (refuse_undeclared_writes) only
  gated_call              -> plan_gate.py (enforce_plan_approval) only
  autonomy_for            -> plan_gate.py (gate_applies) only
  PLAN_ONLY / plan_only    comparison sites in src/: 1
  ```

  (`langgraph_agent.py:49` and `graph_stream.py:117` mention several of these by name in prose only — they are not importers.)
- **Fix**: inline the four pure f-string builders into their middlewares and delete `plan_gate.gated_call` in favour of calling `side_effecting_call` directly; keep `dry_run_refusal`, `undeclared_write_refusal`, `answered_failure` and `returned_failure_detail`, which carry real branching or a second caller. Behaviour-preserving; `tests/test_langgraph_agent.py` imports `denial_result` and `dry_run_refusal` and would need the assertions re-pointed at the middleware's output, which is the more honest test anyway. Also correct the two comments (`PLAN_ONLY`, `autonomy_for`) to describe one caller, or fold `autonomy_for` into `gate_applies`.

---

## `_Narrowing` is not abstract: a subclass missing `_narrows` fails open

- **Severity**: low
- **Location**: `src/chemclaw/agent/skill_access.py:45-75`
- **Trigger**: define a fourth narrowing that implements `_permits` and forgets `_narrows` (or vice versa).
- **Consequence**: `class _Narrowing:` has no `ABCMeta` metaclass, so `@abstractmethod` is inert at runtime — the class and any incomplete subclass instantiate fine, and the un-overridden method returns `None`. A subclass missing `_narrows` makes `permits()` return `not None or …` → `True`, i.e. **every skill visible**, on the one narrowing the module describes as having a security posture (`RoleScopedSkills`). The base class is presented as enforcement (*"A subclass says only whether it is configured to narrow at all …"*) and enforces nothing at runtime.
- **Evidence**:

  ```
  $ uv run python -c "from chemclaw.agent.skill_access import _Narrowing; print(type(_Narrowing)); print(_Narrowing().permits('x'))"
  <class 'type'>
  True
  ```

  A subclass implementing only `_narrows` returns `None` from `permits` (falsy — fail-closed); one implementing only `_permits` returns `True` for everything (fail-open). Mitigated in practice: `mypy --strict` *does* treat an `@abstractmethod`-bearing class as abstract regardless of metaclass and reports `Cannot instantiate abstract class "Broken" with abstract attribute "_narrows"` (verified on an equivalent standalone file), and `make type` covers every first-party package. So this is caught at the gate today; it is the runtime claim that is false.
- **Fix**: one word — `class _Narrowing(ABC):` with `from abc import ABC, abstractmethod`. Behaviour-preserving for every existing subclass; makes the runtime match the docstring.

---

## Checked and found sound (no finding)

- `AuthorizationError`'s docstring claims registration by exact class name in `durable/publish._BAD_DATA_TYPES` and a test that walks the hierarchy. Verified: all four of `AuthorizationError`, `DryRunRefusal`, `PlanNotApprovedError`, `UndeclaredWriteRefusal` are listed (`publish.py:119-128`), and `tests/test_publish.py::test_every_authorization_error_subclass_is_listed_non_retryable` walks `__subclasses__()` after importing the whole tree.
- `side_effecting_tools()` being rebuilt per tool call is *not* a cost: measured 9.6 ms per 1000 calls (both registries are `@cache`d). An earlier 2.4 ms/call reading was a first-call import artifact and does not reproduce.
- `NarrowedSkillsBackend`'s first-path-segment predicate is correct under `CompositeBackend`: the composite strips the route prefix before delegating (`_get_backend_and_key` → `_route_for_path` returns `stripped_key`), so `parts[0]` really is the skill directory and not the tree label.
- `audit_store.PostgresAuditSink` and `turn_flags` are the right size for what they do; nothing to simplify.
