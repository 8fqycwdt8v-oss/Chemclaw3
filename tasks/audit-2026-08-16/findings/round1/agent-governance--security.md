# agent governance — security and hardening

Slice: `src/chemclaw/agent/{authz,tool_authz,plan_gate,skill_access,skill_backend,tool_invocation,audit,audit_store,turn_flags}.py`.
All line numbers are against `HEAD` (`71f03024`).

**Environment note for triage:** while this pass ran, an external mutation harness was writing and
reverting mutants into `src/chemclaw/agent/skill_backend.py` (two observed: `_allows` → `return True`,
and `download_files` dropping its filter, each labelled `# MUTANT`). Those are not repo defects and
are not reported below; every finding here was re-anchored against `git show HEAD:` and re-measured.

---

## An operator gate spelled with an empty role list opens the tool to everyone, including under the `deny` allowlist

- **Severity**: high
- **Location**: `src/chemclaw/agent/authz.py:329-336` (`authorize_tool`), via `_has_required_role` at `:283-292`
- **Trigger**: `entra_required=true`, `tool_authz_default="deny"`, and one operator entry
  `tool_role_gates = {"compute_dft_energy": []}` — the natural spelling of "no role may call this".
  Any authenticated user with zero app roles then calls `compute_dft_energy`.
- **Consequence**: fails **open**. `settings.tool_role_gates.get(tool)` returns `[]`, which is not
  `None`, so the explicit-gate branch is taken; `_has_required_role(frozenset())` returns `True`
  ("no specific role needed"). The gate returns, and the `deny` default below it is never reached —
  so the one entry an operator wrote to *close* a tool is the only thing in the deployment that
  *opens* it, past an otherwise deny-all allowlist. The affected tool in the repro is the HPC/DFT
  launcher, which is also a `DEFAULT_WRITE_TOOL_GATES` member, i.e. the tool the built-in policy
  singles out as most dangerous.
- **Evidence**: the same module applies the opposite rule twice, explicitly, two dozen lines apart —
  `authz.py:348-350` ("An empty privileged set means fail closed, not open: `_has_required_role`
  treats 'no roles required' as satisfied, which is right for operator gates but would silently void
  the built-in write gate") and `authz.py:377-384`. The exemption granted to "operator gates" is
  exactly the case that fails open. `core/config/entra.py:166-171` validates the analogous mistake
  for `entra_expensive_actions` and reasons about it in the ADR voice — there is no equivalent check
  on `tool_role_gates`, and `Field(default_factory=dict)` with `dict[str, list[str]]` accepts `[]`.
  The sibling gate for skills fails **closed** on the identical input
  (`skill_access.py:169-179`: `bool(roles & frozenset())` is `False`), so the same config idea has
  two opposite meanings in two modules.

  Measured (`/tmp/t11.py`, `/tmp/t12.py`):

      default: deny gates: {'compute_dft_energy': [], 'gather_evidence': ['Chemclaw.User']}
      actor = attacker-oid, roles = frozenset()
        compute_dft_energy:  ALLOWED
        gather_evidence:     refused (… holds none of the roles this tool requires)
        propose_knowledge_note: refused (… only an approved list of tools, and this one is not on it)

      RoleScopedSkills({"secret-skill": [], …}) with roles={'Chemclaw.Admin'}
        secret-skill visible: False      # skills fail closed on []
        admin-skill  visible: True

- **Fix**: treat an empty explicit gate as "nobody", not "everybody". In `authorize_tool`:

      required = settings.tool_role_gates.get(tool)
      if required is not None:
          if not required or not _has_required_role(frozenset(required)):
              raise AuthorizationError(...)
          return

  and add a `model_validator` in `core/config/entra.py` rejecting an empty list value in
  `tool_role_gates` at startup, so the ambiguity cannot be shipped at all. Leave
  `_has_required_role`'s "empty means satisfied" semantics alone — three callers depend on it — and
  make the emptiness decision at each call site, as the two built-in gates already do.

---

## The plan-approval gate is inert wherever the ambient session id is unset — the CLI is one such front door

- **Severity**: high
- **Location**: `src/chemclaw/agent/plan_gate.py:344-349` (`enforce_plan_approval`)
- **Trigger**: `harness_enabled=true` (autonomy defaults to `plan_only`), then run a turn through
  any caller of `build_langgraph_agent` that does not call `set_current_session_id` — which is every
  caller except `api/runner.py`. `chemclaw.cli.chat` is one: it passes the session id to LangGraph as
  `thread_id` in `turn_config(session_id)` (`cli/chat.py:118-136`) and never stamps the contextvar.
- **Consequence**: `get_current_session_id()` returns `""`, the gate returns `await handler(request)`
  **before** any approval is consulted, and every state-changing tool runs unapproved — including
  `propose_knowledge_note`/`record_confirmed_answer`, which push a branch to the knowledge repo.
  This is the DARK-1 sequence the module was written to close, reproduced on the other front door.
  It is not mitigated by RBAC on that path: the CLI runs `entra_required=false` by construction
  (`cli/chat.py:11-18`), so `authorize_tool` is a no-op there and the plan gate is the *only*
  control over state-changing tools.
  The CLI meanwhile ships `/plan` and `/approve` commands that write real
  `plan_approvals` rows and print `"approved <hash>; the session may now execute"`
  (`cli/chat.py:240-289`) — an approval ritual in front of a gate that never fires.
- **Evidence**: `grep` for the setter finds exactly one writer:

      src/chemclaw/api/runner.py:187:    session_token = set_current_session_id(session.session_id)

  Driving the real middleware with the two ambient shapes (`/tmp/t13.py`):

      harness_enabled: True autonomy: plan_only gate_applies: True

      [A] CLI shape: no ambient session id
         result: TOOL BODY RAN — a knowledge-graph write happened
      [B] front-door shape: ambient session id set by api/runner.py
         refused: PlanNotApprovedError propose_knowledge_note changes stored data or starts work,
                  and the plan it is part of has not been approved yet …

  The claim this contradicts is `plan_approval_store.py:41-45`: "`session_store="memory"` is a real
  deployment (the CLI is one), and a control with two implementations that disagree about when an
  approval is spent is a control nobody can reason about" — the in-memory backend exists *so that*
  "the harness gate holds there rather than being waived". It does not hold there. The comment at
  `plan_gate.py:345-347` ("No session means no plan to approve and no autonomous loop to gate — a
  template activity's tool step, or a one-shot CLI call. Not a hole: those paths still pass through
  `enforce_tool_authz` and `authorize_trigger`") is also wrong for the CLI specifically, because
  those two gates are open when `entra_required` is false, which is the CLI's only supported mode.
- **Fix**: stamp the ambient session id where the thread id is chosen, not only in the HTTP runner —
  `cli/chat.converse` should `set_current_session_id(session_id)` / reset around `agent.ainvoke`,
  which is the same one-line pairing `api/runner.py:187/591` does. Then change the "no session"
  branch from *skip* to *skip only when the profile's gate does not apply*, or better, make it fail
  closed for gated calls: a side-effecting call with no session under `gate_applies` has no
  approval by definition, and `plan_approval_refusal` already words that.

---

## `writes_durable_memory` misses the one spelling the composite backend also routes to the durable store

- **Severity**: medium
- **Location**: `src/chemclaw/agent/authz.py:198-231` (`writes_durable_memory`, line 231
  `return path.startswith(MEMORY_ROOT)`), consumed by `side_effecting_call` (`:234-246`),
  `tool_authz.dry_run_refusal` (`tool_authz.py:95-107`) and `plan_gate.gated_call` (`plan_gate.py:115-122`)
- **Trigger**: a deployment with the memory route enabled (`store` configured and an ambient actor,
  `scratchpad.scratchpad_backend`), on a turn the caller marked `dry_run=true` (or a `plan_only`
  session with no approved plan). The model calls `write_file(file_path="/memories", content=…)` —
  no trailing slash.
- **Consequence**: both write gates fail open on a durable write. `MEMORY_ROOT` is `"/memories/"`,
  so `"/memories".startswith(MEMORY_ROOT)` is `False` → `side_effecting_call` says the call changes
  nothing outside the turn → `refuse_writes_on_dry_run` lets it through and `enforce_plan_approval`
  never gates it. `CompositeBackend._route_for_path` routes it to the `StoreBackend` anyway, because
  it matches the route root without the trailing slash and rewrites the key to `"/"`. So a turn that
  answered "DRY RUN — nothing was started" has written a row that outlives the session, the process
  and the deployment, into a per-actor namespace that is later read back into the prompt.
- **Evidence**: the docstring at `authz.py:203-215` argues the gate must read the *argument* and
  that "treating an unreadable argument as the ungated case is how a gate becomes bypassable by
  malformed input" — the malformed input it misses is a well-formed path one character short.

  Routing measured against the installed backend (`/tmp/t4.py`, `/tmp/t5.py`):

      '/memories/a.md'  -> backend=StoreBackend  key='/a.md'  durable_per_authz=True   side_effecting_call=True
      '/memories'       -> backend=StoreBackend  key='/'      durable_per_authz=False  side_effecting_call=False
      '/memories/'      -> backend=StoreBackend  key='/'      durable_per_authz=True   side_effecting_call=True

      CompositeBackend.write("/memories", "exfiltrated on a dry run")
        -> WriteResult(error=None, path='/memories')
        -> store contents: [(('memories','actor-digest'), '/', {'content': 'exfiltrated on a dry run', …})]

  **The outer net that would have caught it is not installed.** `scratchpad.filesystem_permissions()`
  denies `/memories` (measured: `_check_fs_permission(rules,"write","/memories") == "deny"`) and its
  docstring calls itself rules "evaluated before any filesystem operation". They are not evaluated:
  `create_deep_agent(permissions=filesystem_permissions())` builds
  `FilesystemMiddleware(..., _permissions=permissions)`, and `langgraph_agent._middleware` then hands
  `_apply_custom_middleware` its own `FilesystemMiddleware(backend=backend, tools=list(scratchpad_tools()))`
  with **no** permissions; `deepagents.graph._apply_custom_middleware:220-228` replaces by `.name`
  in place. Spying on that call during a real `build_langgraph_agent` (`/tmp/t3.py`):

      call 0: FilesystemMiddleware _permissions=[] tools=['ls','read_file','write_file','edit_file','glob','grep']

  (Root cause of that half sits in `agent/langgraph_agent.py` + `agent/scratchpad.py`, outside this
  slice, but it is what turns the `authz.py` gap from defence-in-depth into a live bypass — and it
  independently voids the deny-everything-else write rule for the whole agent.)
- **Fix**: in `writes_durable_memory`, decide with the same rule the router uses rather than a
  prefix test — `path == MEMORY_ROOT.rstrip("/") or path.startswith(MEMORY_ROOT)` — and pin it with
  a test that asserts the predicate agrees with `CompositeBackend._route_for_path` for every path in
  a table, so the two cannot drift again. Separately, pass `_permissions=filesystem_permissions()`
  on the replacement `FilesystemMiddleware` (or splice by wrapping upstream's instance) and assert
  the built middleware's `_permissions` is non-empty in `tests/test_middleware_order.py`.

---

## `NarrowedSkillsBackend.download_files` filters a positional API, so a gated path comes back carrying another skill's body

- **Severity**: medium
- **Location**: `src/chemclaw/agent/skill_backend.py:147-160` (`download_files`)
- **Trigger**: any caller that batches a gated path with permitted ones through the composite —
  `backend.download_files(["/skills/secret-skill/SKILL.md", "/skills/public-skill/SKILL.md"])` for a
  turn whose `permits` predicate refuses `secret-skill`.
- **Consequence**: the gate returns *wrong data with no error* instead of refusing. `download_files`
  is positional — the protocol states "one per input path… order matches input", and
  `CompositeBackend.download_files:900-924` zips the batch responses against the original indices —
  so dropping an element shifts every later response one slot up. The gated path is answered with
  the **next permitted skill's full body** and `error=None`, and the permitted path is answered
  empty. No gated content is disclosed (only permitted bodies move), but a role-gated skill is
  handed to the model as though it existed and were readable, and a legitimately visible skill is
  silently blanked.
- **Evidence**: the docstring asserts the opposite of what the API does — "Filtered rather than
  refused outright: this returns per-path results, so a caller asking for five paths of which one is
  gated should get the four, exactly as `glob` and `grep` do." `glob`/`grep` return a flat match list
  where dropping an element is meaningful; `download_files` does not.

  Measured (`/tmp/t9.py`, two real skill dirs, `permits = lambda n: n != "secret-skill"`):

      path= /skills/secret-skill/SKILL.md | error= None | content= b'---\nname: public-skill\ndescription: PUBLIC BODY\n---\n'
      path= /skills/public-skill/SKILL.md | error= None | content= b''

  Latent today only because `SkillsMiddleware._list_skills_with_errors` pre-filters via `ls` before
  calling `download_files` (`deepagents/middleware/skills.py:613-641`) — which is precisely the
  assumption the docstring says this override exists to stop depending on ("it becomes live the
  moment upstream fetches a body this way. That is a patch release in a 0.x package").
- **Fix**: keep the arity and refuse per path, mirroring `read`:

      def download_files(self, paths: list[str]) -> Any:
          allowed = [p for p in paths if self._allows(p)]
          fetched = {r.path: r for r in super().download_files(allowed)}
          return [fetched.get(p) or FileDownloadResponse(path=p, content=None, error=REFUSED)
                  for p in paths]

  and add a test asserting `len(result) == len(paths)` and `result[i].path == paths[i]` with a gated
  path in the middle of the batch.

---

## `authorize_tool` has no reject-if-absent rule, so authorization and attribution can read different principals

- **Severity**: low
- **Location**: `src/chemclaw/agent/authz.py:295-354` (`authorize_tool`); contrast `authorize_trigger`
  `:373-375` and `require_actor` `:408-412`
- **Trigger**: any governed tool call made under `entra_required=true` with the ambient identity
  unset. `authorize_tool` never asks whether there *is* an actor: with `tool_authz_default="allow"`
  (the default) every ungated tool runs for the null principal, and only the four
  `DEFAULT_WRITE_TOOL_GATES` names are refused.
- **Consequence**: not exploitable through the HTTP front door today — `require_principal` guarantees
  a principal, and `durable/template_activities.py:180/260/388` stamps identity from the step before
  calling `invoke_governed`. It is a missing invariant rather than a live hole, and the shape it
  guards against is real in this code: `tool_invocation.invoke_governed` takes `actor` as a
  *parameter* used only for the audit row (`audit.py:337` — `get_current_actor() or actor`), while
  the gate one layer down reads the contextvar. A caller that passes `actor="alice"` and forgets
  `set_current_identity` produces audit rows attributing to alice for calls authorized against
  nobody — the D-040 shape `audit.py:104-108` names as worse than an unrecorded act.
  Measured: with `entra_required=true`, an authenticated role-less actor is allowed 20 of the 28
  tools in `side_effecting_tools()` (`/tmp/t8.py`), and the null principal is allowed exactly the
  same 20 — no gate distinguishes them.
- **Evidence**: `authorize_tool` contains no `get_current_actor()` call at all; the refusal builder
  `_actor()` (`:278-280`) renders `"an unauthenticated user"`, i.e. the module already knows the
  null case reaches it, and only formats it rather than refusing it. `authorize_trigger` handles the
  same case at `:373-375` with `raise AuthorizationError(f"{action} requires an authenticated user")`.
- **Fix**: add the reject-if-absent line to `authorize_tool` under `entra_required` (one call to
  `get_current_actor()`, refusing `None` before consulting any gate), and make `invoke_governed`
  stamp the identity it was handed rather than trusting its caller to have done it — so the row it
  writes and the gate it runs can never name different principals.

---

## What held up

Reported so the triage knows these were checked rather than skipped:

- **The skills read gate is sound on every reach path.** `read`, `aread`, `ls`, `glob`, `grep` and
  their async twins all refuse or drop a gated skill through the composite (`/tmp/t10.py`); the
  async twins do dispatch through the subclass (`BackendProtocol.als/aread/adownload_files` are
  `asyncio.to_thread(self.<sync>, …)`, and `FilesystemBackend.agrep` calls `self.grep`). Path
  traversal out of a permitted skill is blocked upstream in `virtual_mode` by a substring test on
  `".."` plus a `relative_to(cwd)` check after `resolve()`, so `_allows`'s "first segment is the
  skill" shortcut cannot be walked around.
- **No SQL injection in the slice.** `audit_store._INSERT` and `plan_approval_store`'s three
  statements are fully parameterised, with no identifier or `WHERE` fragment interpolated.
- **Audit sizes are bounded** where model-controlled: `arguments`, `detail` and returned-error text
  all pass `_truncate` (`agent_audit_max_arg_chars`, default 200), and `failure_detail`/
  `returned_failure_detail` cap at 300 chars.
- **Session-scoped resources are ownership-checked at the boundary** — `api/deps._refuse_unless_owner`
  404s a non-owner before `plan.get_plan`/`decide_plan` run, so the `(session_id, plan_hash)` key the
  plan gate trusts cannot be aimed at another user's approval.
- **Roles from group claims are namespaced.** `api/auth._principal_from_claims` does apply
  `GROUP_ROLE_PREFIX`, so a directory group named identically to a privileged app role does **not**
  satisfy `_has_required_role` — verified by constructing the principal and running both gates
  (`/tmp/t14.py`, principal roles `{'group:Chemclaw.Admin'}`, both privileged tools refused).
- **Not reported as a defect, but worth an operator's eye:** on the shipped posture
  (`entra_required=true`, `tool_authz_default="allow"`, both role settings empty), 20 of the 28 tools
  in `side_effecting_tools()` are callable by any authenticated role-less user, including five
  durable xTB job launchers and `report_measurement`. That is the documented design — writes are
  closed only for `DEFAULT_WRITE_TOOL_GATES` and jobs declaring `expensive: true` — but the gap
  between "side-effecting" (28) and "closed by default" (8) is larger than the module's prose
  suggests, and it is entirely a policy choice an operator must make in `tool_role_gates`.
