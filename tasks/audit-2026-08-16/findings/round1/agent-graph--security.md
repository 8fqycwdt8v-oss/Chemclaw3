# Round 1 — `agent/` graph slice, security & hardening lens

Slice: `src/chemclaw/agent/{langgraph_agent,chemclaw_agent,checkpointer,compaction,loop_cap,graph_tools,llm_provider}.py`

Everything below was run against the live tree (`uv run`, Postgres up via `make up`). Claims made in
docstrings were checked against behaviour; where the two disagree that is called out.

Things I checked and found **sound** (so a later round does not re-spend the time): note `id`/`type`
are slug-validated before they become a file path and a git ref, so `propose_knowledge_note` has no
traversal (`kg/note.py:363`, `_SLUG = ^[A-Za-z0-9][A-Za-z0-9_.-]*$`); `render_note` goes through a
real YAML dumper, so a model-supplied `source` containing `\ncreated_by: human\n` round-trips as a
quoted scalar and cannot forge frontmatter (verified); `request.system_message` is a separate field
from `request.messages`, so the compaction window cannot drop the system prompt; the governance
`wrap_tool_call` chain really does sit inside `FilesystemMiddleware` and `SubAgentMiddleware` on both
the main graph and the `task` helper (dumped the compiled list); `disabled_summarizer` really does
occupy upstream's slot (`_trigger_clauses == []`); `scratchpad_tools()` really does withhold
`execute`/`delete` on the compiled instance (`['ls','read_file','write_file','edit_file','glob','grep']`);
session→thread ownership is enforced upstream of the checkpointer by `resolve_session`.

---

## `permissions=filesystem_permissions()` is silently discarded — the write deny-rules never load

- **Severity**: medium
- **Location**: `src/chemclaw/agent/langgraph_agent.py:255` (`permissions=filesystem_permissions()`) together with `src/chemclaw/agent/langgraph_agent.py:308` (`FilesystemMiddleware(backend=backend, tools=list(scratchpad_tools()))`); rules defined at `src/chemclaw/agent/scratchpad.py:235` (`filesystem_permissions`)
- **Trigger**: any `build_langgraph_agent(...)` call — i.e. every turn, under every profile, with no special input.
- **Consequence**: `create_deep_agent` builds its own `FilesystemMiddleware(..., _permissions=permissions)` (deepagents `graph.py`, line ~553) and then calls `_apply_custom_middleware`, whose documented rule is that a custom entry *whose `.name` matches one already present replaces it in place*. This repository's entry shares that name and is constructed **without** `_permissions`, so the replacement drops the rules on the floor. The compiled middleware carries `_permissions == []`, which `_check_fs_permission` treats as "allow everything". The blanket `deny /**` for writes, and the explicitly-argued second refusal of `/skills/`, do not exist at runtime. Upstream's own `_REQUIRED_MIDDLEWARE` docstring calls this "a security guarantee"; `scratchpad.filesystem_permissions` calls it "deny-rules bounding where a turn may write, evaluated before any filesystem operation". Neither is true as compiled.
- **Evidence**:

  ```
  $ uv run python /tmp/probe3.py      # spies deepagents.graph.create_agent, reads the spliced instance
  attrs with perm: ['_permissions']
      _permissions = []
  ```

  and the rules that *would* have applied, evaluated with upstream's own checker:

  ```
  $ uv run python -c "...  _check_fs_permission(rules,'write',p)  vs  _check_fs_permission([],'write',p)"
  /scratch/a.txt           declared-rules='allow'   as-compiled(empty)='allow'
  /memories/x              declared-rules='allow'   as-compiled(empty)='allow'
  /anywhere/evil.md        declared-rules='deny'    as-compiled(empty)='allow'
  /skills/x/SKILL.md       declared-rules='deny'    as-compiled(empty)='allow'
  ```

  Residual defence, so this is medium and not high: `/skills/…` writes still raise from
  `NarrowedSkillsBackend.write` (`skill_backend.py:162`), and every unrouted path falls through to
  `StateBackend`, i.e. graph state rather than a disk. So today the loss is the *outer* half of a
  deliberately two-layer control plus the `deny /**` catch-all — exactly the layer
  `scratchpad.filesystem_permissions`'s docstring says exists because "a security property that
  arrives as somebody else's default can leave the same way". The moment a fourth route is added to
  `scratchpad_backend`, or upstream changes a backend default, nothing is behind it.

  The `helper=True` branch (`langgraph_agent.py:250`, `create_agent(**shared)`) has the same
  property for a different reason — `create_agent` takes no `permissions=` at all — so both graphs a
  turn compiles are unbounded here.

- **Fix**: construct the middleware with the rules: `FilesystemMiddleware(backend=backend, tools=list(scratchpad_tools()), _permissions=filesystem_permissions())`, and keep passing `permissions=` to `create_deep_agent` (upstream still reads the *argument* to build `HumanInTheLoopMiddleware` from any `interrupt`-mode rule, at line ~603). Then assert it: `tests/test_middleware_order.py` already captures the spliced list, so one line there — `assert fs._permissions == filesystem_permissions()` — turns this from a review property into a red build. The underscore-prefixed name is upstream's, which is itself worth pinning in `tests/test_upstream_surface.py`.

---

## `expand_note` bounds the hop count and not the result count — one call returns the whole graph

- **Severity**: medium
- **Location**: `src/chemclaw/agent/graph_tools.py:168-178` (`expand_note`, the `hops` clamp and the `neighbors` comprehension)
- **Trigger**: the model calls `expand_note(note_id="<any well-connected note>", hops=3)`. `hops` is a model-controlled argument, `graph_max_hops` defaults to 3, and both the note id and the hop count are things retrieved (untrusted) content can steer the model toward.
- **Consequence**: `neighborhood()` is an undirected BFS with `cutoff=hops`; clamping `hops` bounds the *depth* and not the *breadth*, so on any small-world corpus three hops from a hub is the whole graph. Every reachable note becomes a `NoteRef` in one tool result, unbounded and uncapped, and goes straight into the model's context. The sibling discovery tool on the same module *is* capped (`find_notes`, `settings.graph_max_results`, default 50) and warns on truncation; this one is not. Above the context budget the outcome is either a provider hard error or a full-price call, and `ClearToolUsesEdit` keeps the newest tool results verbatim so a single oversized result cannot be reclaimed by compaction.

  The inline comment at :168-170 asserts the property that is missing: *"clamp it to [0, graph_max_hops] so a large value is bounded rather than traversing the whole graph (SEC-4)"*. Measured, `hops=3` traverses the whole graph.
- **Evidence**: synthetic corpus of 801 notes, 800 of them linking `[[hub]]`, `CHEMCLAW_KNOWLEDGE_DIR` pointed at it:

  ```
  knowledge_path /tmp/kgrepo/kb max_results 50 max_hops 3
  expand_note neighbors returned: 800
  serialized bytes: 124163
  find_notes capped at 50 matches (id order) for 'body text'; ...
  find_notes returned (capped at graph_max_results): 50
  ```

  124 KB ≈ 31k tokens from one tool call, against `agent_context_token_budget = 100_000`. On the
  shipped 38-note corpus the same shape already shows: worst-case 3-hop neighbourhood is 18 of 38
  nodes (47%).
- **Fix**: apply `settings.graph_max_results` to `neighbors` the way `find_notes` applies it — truncate in the existing `sorted(...)` order and log the same truncation warning, so the cap is never silent. (Sorting already makes the truncation deterministic.) If the anchor's full neighbourhood is genuinely wanted, that is a paged tool, not an uncapped one.

---

## A connector tool silently shadows an in-process tool of the same name, inheriting its governance identity

- **Severity**: medium
- **Location**: `src/chemclaw/agent/langgraph_agent.py:221` (`bound = [*tools, *(connectors or [])]`)
- **Trigger**: an enabled connector's manifest declares an endpoint tool whose name equals an in-process `@tool` (or a template launcher). No agreement or check refuses it; the connector entry comes second in the list, and LangChain's `ToolNode` builds `tools_by_name` last-wins.
- **Consequence**: the out-of-process tool *replaces* the first-party one in the executor while keeping its name — and every gate in the chain keys on the name, not on the object. `enforce_tool_authz` calls `authorize_tool(request.tool_call["name"])` (`tool_authz.py:238`); `refuse_writes_on_dry_run` and `enforce_plan_approval` both test membership in `authz.side_effecting_tools()`, a *set of names*; the audit row records the name. So a connector tool named `find_notes` (classified read-only, ungated, not plan-gated, not dry-run-refused) can do whatever the connector process does, and the trail says `find_notes` ran. The dangerous direction is precisely this one: shadowing a *read* name with a *write* capability escapes three gates at once.

  This asymmetry is the tell rather than the theory. `connectors/registry.py:571-586` refuses two connectors declaring the same **job** name, with exactly the right reasoning written into the docstring — *"the name is the authorization key, so a collision would silently make one connector's gate apply to the other's work"*. The same argument applies verbatim across the endpoint/in-process boundary and is not enforced there. `available_tool_names()` unions six name spaces into a `set`, so a collision is invisible to every validator built on it, and `_register_generated_tools` (`chemclaw_agent.py:499-502`) silently *skips* a colliding launcher rather than reporting it.
- **Evidence**: no collision exists in the shipped config today (`set(registered_tool_names()) & set(endpoint_tool_names()) == []`, 28 vs 30 names), so this is a latent hole rather than a live one. The shadowing itself is real:

  ```
  $ uv run python /tmp/probe4.py     # passes a connector tool named find_notes into build_langgraph_agent
  tools_by_name has find_notes: True
  resolved find_notes description: SHADOW: an MCP connector tool claiming an in-process tool's name.
  ```

  Reachability is bounded by the manifest — `_served_tool_problems` pins a bundle's served set to its
  declared `tools`, and `transport.py:188` filters the live server's tools to `allowed_tools` — so
  the entry point is a manifest edit in a companion repo (`Chemclaw3-mcp`, `Chemclaw3_mock`), not a
  runtime attacker. That is why this is medium and not high; it is still a one-line-to-close
  authorization-key collision in a tree that closes the identical one for jobs.
- **Fix**: in `build_langgraph_agent`, refuse rather than merge — raise naming both providers when `{t.name for t in connectors} & {t.__name__ for t in tools}` is non-empty, mirroring `job_tools()`'s message. Better still, add the check to `make connector-validate` so it fails in CI rather than at the first turn after a deploy.

---

## The checkpointer/memory-store pool has no `statement_timeout`, breaking the "bounded by default" invariant

- **Severity**: medium
- **Location**: `src/chemclaw/agent/checkpointer.py:367-373` (`_checkpoint_pool`, the `kwargs={"autocommit": True, "connect_timeout": ...}` dict); the same pool is handed to `AsyncPostgresStore` at `src/chemclaw/agent/scratchpad.py:153`
- **Trigger**: any checkpointer or memory-store statement that blocks — a lock wait behind a retention `DELETE` over `checkpoints`/`checkpoint_blobs`/`checkpoint_writes`, or a large-thread read. `api/runner` awaits a checkpointer round-trip once per turn, so this is the hot path, not a corner.
- **Consequence**: the statement is never cancelled server-side. `core/config/store.py:47-53` states the deployment invariant this pool breaks: `pg_statement_timeout_seconds` is *"Applied by `db.connection()` to every borrowed connection whose caller names no bound of its own, so a store cannot be unbounded by forgetting an argument"*. `_checkpoint_pool` does not go through `db.connection()`; it constructs its own `AsyncConnectionPool` and passes only `autocommit` and `connect_timeout`. With `pg_pool_max_size = 16`, sixteen blocked turns exhaust the checkpointer pool and every subsequent turn fails at `getconn` — while the blocked statements sit on the server indefinitely. `checkpointer.py`'s module docstring gives three carefully measured reasons for a separate pool; none of them is "and it should be unbounded", so this reads as a control lost in the split rather than a decision.
- **Evidence** (Postgres up via `make up`):

  ```
  checkpointer/store pool statement_timeout = ('0',)
  shared db.connection() statement_timeout = ('30s',)
  ```

- **Fix**: add the bound to the pool's connection options rather than to each call site, so the separate pool keeps the property the shared one has: `kwargs={"autocommit": True, "connect_timeout": settings.pg_connect_timeout_seconds, "options": f"-c statement_timeout={int(settings.pg_statement_timeout_seconds * 1000)}"}` (reuse `core/db._merged_options` so the string is written once). `setup()`'s `CREATE INDEX CONCURRENTLY` runs on the same pool and can legitimately exceed 30 s, so run the migration on a connection that clears the bound — the same distinction `pg_migration_lock_timeout_seconds` already draws for migrations.

---

## `_labelled`'s de-duplication can still collide, silently dropping a skills tree from the routing table

- **Severity**: low
- **Location**: `src/chemclaw/agent/langgraph_agent.py:540-561` (`_labelled`), consumed at `:481` (`sources=`) and `:525` (`routes=`)
- **Trigger**: three skills trees whose derived labels are `foo`, `foo-1`, `foo` — e.g. a configured root or bundle producing `foo`, a bundle literally named `foo-1`, and a second bundle producing `foo`. The suffix scheme assigns the third the label `foo-1`, which the second already holds.
- **Consequence**: `skills_backend` builds `routes` as a dict comprehension keyed on `f"/{label}/"`, so the later tree wins and the earlier one becomes unreachable — while `_skills_middleware` still publishes *both* under `/foo-1`, so the system prompt advertises skill paths for tree B that resolve into tree C's files. The docstring asserts the opposite: *"a numeric suffix settles anything still colliding, which keeps the function total rather than correct-until-someone-nests-two-trees-alike"*. It does not; the suffix is generated without checking whether the suffixed name is itself taken.

  The role gate is not bypassed by this — `NarrowedSkillsBackend._allows` keys on the path's first
  segment (the skill directory name) and is applied by whichever backend actually serves the read —
  so this is a routing/availability defect and a false statement in a docstring, not an escalation.
  That is why it is low.
- **Evidence**:

  ```
  $ uv run python -c "from chemclaw.agent.langgraph_agent import _labelled; ..."
  [('foo', 'a/foo'), ('foo-1', 'b/foo-1'), ('foo-1', 'c/foo')]
  routes: {'/foo/': 'a/foo', '/foo-1/': 'c/foo'}
  collision lost a tree: True
  ```

- **Fix**: loop the suffix until the candidate is unused (`while candidate in used: count += 1`), tracking emitted labels rather than base counts. One `used: set[str]` alongside `seen`.

---

## `find_notes` writes the chemist's raw query text into the log stream

- **Severity**: low
- **Location**: `src/chemclaw/agent/graph_tools.py:116-122`
- **Trigger**: any `find_notes` call that hits the `graph_max_results` cap (50 by default) — which is the *broad* query, i.e. exactly the one most likely to carry a compound name, a project codename or a chemist's phrasing of an unpublished route.
- **Consequence**: the query string is emitted at WARNING via `%r` and lands in whatever aggregator the pod ships logs to. The global `SecretRedactingFilter` (`core/logging.py:757`) redacts *credentials*, not user content, so nothing strips it. This is inconsistent with the deployment's own stated posture for the other content channel: `otel_include_sensitive_data` defaults to `False` precisely so prompt/response content does not leave the process, and the model-call spans honour it. One channel is gated by a setting; this one is unconditional.
- **Evidence**: observed during the `expand_note` measurement above, unprompted:

  ```
  find_notes capped at 50 matches (id order) for 'body text'; narrow the query or raise CHEMCLAW_GRAPH_MAX_RESULTS
  ```

- **Fix**: log the shape, not the content — `log.warning("find_notes capped at %d matches for a %d-term query; …", cap, len(terms))` — or gate the `%r` behind the same `settings.otel_include_sensitive_data` switch that governs the other content channel, so there is one answer to "does chemist text leave this process".
