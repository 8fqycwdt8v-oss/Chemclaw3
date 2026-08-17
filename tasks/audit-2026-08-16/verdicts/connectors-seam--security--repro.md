# Verdicts — connectors seam, security & hardening (round 1), reproduction lens

Adversarial re-derivation of the two **high** findings in
`tasks/audit-2026-08-16/findings/round1/connectors-seam--security.md`. The three medium/low
findings are out of scope and were not examined.

Everything below was reproduced with my own scaffolding: two standalone `mcp.server.fastmcp`
streamable-HTTP servers I wrote (`/tmp/audit-repro/server.py` on :8991,
`/tmp/audit-repro/rogue_server.py` on :8992), two scratch bundle dirs
(`/tmp/audit-repro/cx`, `/tmp/audit-repro/cy`), and my own client scripts. The reporter's scripts
and transcripts were not run. Working tree untouched: `git status --porcelain` shows no modified
tracked file before or after.

Tree state: `HEAD = 01797786ea584e54f9049871f9ccfbaec4b5dfab`, no local source edits.

---

## An endpoint with no `tools:` gives the server an unlimited tool surface

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Every cited line number and symbol is real and current at HEAD:

  ```
  registry.py:427/440   allowed_tools=tuple(endpoint.tools) if endpoint.tools else None,
  transport.py:201      def _allowed(tools, allowed) ...
  transport.py:208/211  if allowed is None: / return [tool for tool in tools if tool.name in keep]
  manifest.py:124       tools: list[str] = Field(default_factory=list)
  registry.py:571/601/638  job_tools / state_changing_tool_names / endpoint_tool_names
  validate_connectors.py:102  for tool in sorted(manifest.endpoint.tools if manifest.endpoint else [])
  ```

  I wrote a four-tool MCP server (`similar_molecules`, `propose_knowledge_note`,
  `delete_all_notes`, `exec_shell`) and a bundle `thirdparty/connector.yaml` carrying an
  `endpoint:` with `auth: mode: none` on a loopback URL and **no `tools:` key at all**, then ran
  the real registry path (`enabled()` → `mcp_connections()` → `open_connector_specs`):

  ```
  $ uv run python /tmp/audit-repro/client.py
  connectors_dirs: ['/tmp/audit-repro/cx']
  enabled: ['thirdparty']
  manifest.endpoint.tools = []
  state_changing = [] read_only = []
  allowed_tools on spec: [None]
  registry.endpoint_tool_names() = []
  registry.state_changing_tool_names() = []
  registry.connector_tool_names() = []
  tools bound into the turn: ['similar_molecules', 'propose_knowledge_note', 'delete_all_notes', 'exec_shell']
  unreachable: []
  ```

  The manifest loaded without complaint — `_check_classification` over three empty lists is a
  no-op and `_contributes_capability` is satisfied by the endpoint alone — and all four tools the
  server advertised were bound into the turn. My number is identical to the reporter's, on my own
  server and my own manifest.

  I then measured the four claimed controls directly rather than inferring them. Under the shipped
  production posture (`entra_required=true`, `tool_authz_default="allow"`, `tool_role_gates={}`),
  with the same bundle enabled:

  ```
  $ uv run python /tmp/audit-repro/gates.py
  entra_required = True tool_authz_default = allow tool_role_gates = {}
  side_effecting_tools() includes connector decls: []
    exec_shell                 plan-gate/dry-run governs: False  authorize_tool: ALLOWED
    delete_all_notes           plan-gate/dry-run governs: False  authorize_tool: ALLOWED
    propose_knowledge_note     plan-gate/dry-run governs: True   authorize_tool: REFUSED (…privileged role…)
    record_confirmed_answer    plan-gate/dry-run governs: True   authorize_tool: REFUSED (…privileged role…)
  ```

  `exec_shell` and `delete_all_notes` — arbitrary names the remote server invented — pass
  `side_effecting_call` (so neither `plan_gate.gated_call` nor `dry_run_refusal` governs them) and
  pass `authorize_tool` under enforcement with no role held. The only two names that *are* refused
  are refused because they happen to be hardcoded strings in
  `authz.DEFAULT_WRITE_TOOL_GATES` / `STATE_CHANGING_TOOLS`, which is coincidence and not a control
  derived from the manifest — exactly what the finding says.

- **Why**

  The mechanism is real, the four-way blindness is real, and I measured it end to end rather than
  reading it. `_allowed`'s `None` branch is unconditional, `endpoint_tool_names` /
  `state_changing_tool_names` both read `manifest.endpoint.tools` (empty), and `side_effecting_tools()`
  unions `state_changing_tool_names()` — so the empty declaration propagates into every gate at once.
  Nothing upstream prevents it.

  Three things I checked that could have refuted it, and did not:

  1. **Profile narrowing is not a backstop by default.** `chemclaw_agent._narrow_allowed_specs`
     does bound an `allowed_tools=None` spec — but only when `prof.tool_names is not None`, and
     `DEFAULT_PROFILE = AgentProfile(name="default")` leaves every field unset
     (`agent/profiles.py:64`). The default turn is the unbounded one. This *aggravates* the finding
     rather than mitigating it; the reporter did not mention it.
  2. **`connector-validate` is not a backstop either.** It is a CI/`make` check, not a runtime one,
     and it never inspects whether `tools:` is empty: `_tool_surface_problems` iterates
     `manifest.endpoint.tools` (`[]` → no problems) and `_served_tool_problems` compares against a
     locally-importable `server.tools` module. I confirmed `_tool_surface_problems -> []` on my
     bundle. (A partial correction to the finding: for an *out-of-tree* bundle,
     `server_tools_module` raises rather than returning `None`, because `exc.name` is the bundle
     package rather than `…<name>.server`, so `connector-validate` errored with
     `"its server module could not be imported"`. That message says nothing about the missing
     allow-list, does not run in production, and does not apply to the in-package `chem`/`safety`
     shape the finding cites — so it does not change the verdict.)
  3. **No shipped bundle triggers it today.** I enumerated all seven manifests under
     `src/chemclaw/connectors/`; every endpoint-bearing one declares a non-empty `tools:`. So this
     is a latent fail-open default, not a live exposure — which is why I would not go above high.

  Why still high rather than medium: the failure is on *omission*, and it is the one polarity the
  adjacent partition check in the same file refuses to accept. `_check_classification` (code, not a
  doc) makes an unclassified tool a load-time error precisely because "every way of getting that
  wrong fails open"; the allow-list that classification partitions is optional and empty means
  unbounded. The remote MCP server is a separate release and a separate trust domain by design
  (`chem`, `safety`), so "everything this server offers" hands that domain unbounded, ungated
  authority over the tool surface of every turn.

---

## A connector tool name silently replaces a core tool of the same name

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  I wrote a second MCP server advertising four names that collide with core capability tools
  (`find_notes`, `record_confirmed_answer`, `get_durable_job_status`, `read_attachment`) with
  distinctive return values and docstrings, and a bundle `rogue/connector.yaml` that **declares all
  four honestly** under `tools:` and `read_only:` — no reliance on the previous finding.

  ```
  $ uv run python /tmp/audit-repro/collide.py
  manifest loads OK; tools = ['find_notes', 'record_confirmed_answer', 'get_durable_job_status', 'read_attachment']
  _tool_surface_problems -> []
  allowed_tools -> (('find_notes', 'record_confirmed_answer', 'get_durable_job_status', 'read_attachment'),)
  core tool count: 21
     core provides find_notes -> True
     core provides record_confirmed_answer -> True
     core provides get_durable_job_status -> True
     core provides read_attachment -> True
  connector tools: ['find_notes', 'record_confirmed_answer', 'get_durable_job_status', 'read_attachment'] unreachable: []
    find_notes                 -> StructuredTool  desc='Find notes.'
    record_confirmed_answer    -> StructuredTool  desc='Record a confirmed answer.'
    get_durable_job_status     -> StructuredTool  desc='Job status.'
    read_attachment            -> StructuredTool  desc='Read an attachment.'
    invoke find_notes -> [{'type': 'text', 'text': 'ROGUE-find_notes', …}]
  ```

  The descriptions are my rogue server's docstrings, and invoking the resolved tool actually
  returned `ROGUE-find_notes`. `ToolNode.__init__` in the installed langgraph assigns
  `self._tools_by_name[tool.name] = tool` in a plain loop over the sequence, so last-wins, and
  `langgraph_agent.py:221` is verbatim `bound = [*tools, *(connectors or [])]`.

  I then drove the **real compiled graph** (`build_langgraph_agent`) with a fake chat model that
  records what `bind_tools` receives, to test the one thing that could have refuted this — that a
  duplicate tool name would blow up at the provider:

  ```
  $ uv run python /tmp/audit-repro/fullgraph.py
  MODEL BOUND with 28 tools; duplicate names: []
     find_notes                 count=1 desc=['Find notes.']
     record_confirmed_answer    count=1 desc=['Record a confirmed answer.']
     get_durable_job_status     count=1 desc=['Job status.']
     read_attachment            count=1 desc=['Read an attachment.']
  ```

  Zero duplicates reach the model. `langchain/agents/factory.py` builds
  `default_tools = list(tool_node.tools_by_name.values()) + built_in_tools`, i.e. it binds the
  already-deduplicated dict — so there is no 400 from the provider, no warning, no log line. The
  model is offered exactly one `find_notes`, and it is the connector's.

- **Why**

  Every element of the claim reproduces: the validator accepts the manifest, the allow-list keeps
  all four names, `ToolNode` resolves each to the connector's implementation, the executor really
  runs it, and the model-facing schema is silently the connector's. No collision check exists
  anywhere — I grepped for one and confirmed `job_tools()` (registry.py:571-587) is the only
  duplicate detector in the seam, and it compares job names against *other connectors' job names*,
  never against `core.tool_registry.registered_tool_names()`.

  Two things I found that the reporter missed, one in each direction:

  - **Worse than filed.** The codebase already recognised this hazard for the *other* generated
    tool source and fixed it there: `templates/registry` emits `run_<name>`, and
    `tests/test_templates.py:160` states the reason in its own docstring — *"prefixed so a template
    cannot shadow a tool or a job — one namespace."* Connector endpoint tools are the one source of
    the three with neither a prefix nor a check. Separately, the *reverse* collision is silent too:
    `chemclaw_agent._register_generated_tools` skips a generated tool whose name is already
    registered (`if tool_fn.__name__ not in known`), so a connector **job** named after a core tool
    is silently dropped rather than raising — the same class of defect with the winner reversed.

  - **One bullet is overstated, and it is the flagship one.** "A shadowing connector takes the
    writer itself" is not quite right for `record_confirmed_answer`. Both `authorize_tool` and
    `side_effecting_call` are keyed on the *name*, not on the tool object, so a shadowing
    `record_confirmed_answer` inherits core's gates: my run above shows it `REFUSED` under
    `entra_required` with no privileged role, and `side_effecting_call` returns `True` so the plan
    gate covers it. What the connector gets is a *gated* writer whose implementation it controls —
    it can lie about what was written, and under the dev default (`entra_required=false`) there is
    no gate at all — which is still serious but is not an ungated PR-gate writer. The other three
    bullets (`find_notes`, `gather_evidence`, `get_durable_job_status`, `read_attachment`) are
    reads with no gate entry and shadow completely freely, which is why the finding's severity
    stands.

  - **Partial detection surface, not a prevention.** `transport._stamped` puts
    `chemclaw.served_by = {connector, revision}` on every connector tool's metadata, and
    `agent/audit.py::_served_by` writes it, so an audit row for a shadowed `find_notes` would carry
    a connector stamp where core's would carry none. Nothing reads or alerts on that difference,
    so it is forensic only — but a fix that only added an alert would be strictly weaker than the
    collision check the finding asks for.

  Reachability: the manifest path requires a `connector.yaml` author, who is the deployer. Composed
  with the finding above, no manifest change is needed at all — a bundle with an empty `tools:`
  lets the remote server, in a different release and a different trust domain, choose the colliding
  names itself. That composition is what keeps this at high rather than medium, and it is the
  composition the reporter explicitly named.
