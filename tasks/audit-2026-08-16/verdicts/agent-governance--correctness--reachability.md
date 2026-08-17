# Verdicts — agent governance slice, CORRECTNESS lens (reachability/consequence verifier)

Scope: only findings marked **critical** or **high**. The file contains exactly one — the
`file_path` normalization finding. The other three are severity medium/medium/low and were not
examined.

Working-tree note: `src/chemclaw/agent/authz.py` and `src/chemclaw/agent/scratchpad.py` were
diffed against the pristine `HEAD` copy and are byte-identical, so nothing below rests on another
agent's mutation.

---

## The dry-run gate and the plan gate both read the raw `file_path`, but the tool writes the *normalized* one

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (the finding says high — see "Why", last section)

### What I did

**1. The predicate, on the real functions.**

```
$ uv run python /tmp/v1.py        # set_dry_run(True), real authz/tool_authz/plan_gate
'/memories/note.md'      durable=True  side_eff=True  dry_run=REFUSED  plan_gated=True  validated=/memories/note.md
'memories/note.md'       durable=False side_eff=False dry_run=ALLOWED  plan_gated=False validated=/memories/note.md
'/./memories/note.md'    durable=False side_eff=False dry_run=ALLOWED  plan_gated=False validated=/memories/note.md
'/memories//note.md'     durable=True  side_eff=True  dry_run=REFUSED  plan_gated=True  validated=/memories/note.md
'/scratch/x.md'          durable=False side_eff=False dry_run=ALLOWED  plan_gated=False validated=/scratch/x.md
```

**2. End-to-end through the real compiled graph, the real middleware chain, and the live Postgres
store** (`build_langgraph_agent` with `tests/fakes_langgraph.ScriptedChatModel`, real
`memory_store()` over the running Postgres, `agent_memory_enabled=True`, `session_store=postgres`,
ambient actor `alice-oid`, `set_dry_run(True)`):

```
# non-canonical
ToolMessage 'Updated file /memories/note.md'
STORE ITEMS: [(('memories','b58afa8d613a68d3'), '/note.md', "{'content': 'SECRET dry-run leak', ...")]

# control, canonical spelling, same script
ToolMessage 'Refused: DRY RUN — write_file changes stored data or starts work, so it was not called.'
(no new row)

# control, '/./memories/note3.md'
ToolMessage 'Updated file /memories/note3.md'
STORE ITEMS: [... '/note3.md' ...]
```

**3. The plan-gate half, under the shipped chart's posture** (`deploy/helm/chemclaw/values.yaml`
sets `CHEMCLAW_HARNESS_ENABLED: "true"`, `CHEMCLAW_HARNESS_AUTONOMY: "plan_only"`,
`CHEMCLAW_SESSION_STORE: "postgres"`), with an ambient `session_id` and no approval, script
`write_todos(...)` then `write_file(...)` in separate assistant messages:

```
# canonical (control)
ToolMessage 'Refused: write_file changes stored data or starts work, and the plan it is part of
             has not been approved yet; review the plan and approve it, then ask again'
(no new row)

# non-canonical
ToolMessage 'Updated file /memories/pgB.md'
STORE: [('/pgB.md', ...)]
```

My first plan-gate run let *both* spellings through, because `enforce_plan_approval` returns early
when `get_current_session_id()` is empty (`plan_gate.py:344-349`). With the session id set, the
control refuses and the bypass writes. Worth recording so the control is not misread.

**4. Nothing upstream stands in the way.** Checked each candidate:

- `MessageIn.dry_run` (`api/schemas.py:45`) is a plain request field wired to
  `run_turn(dry_run=body.dry_run)` at `api/routes/turns.py:168` — the trigger reaches the gate
  from an ordinary authenticated HTTP POST, no privileged role required.
- `FilesystemMiddleware(backend=backend, tools=list(scratchpad_tools()))` is attached
  **unconditionally** (`langgraph_agent.py:308`), so `write_file`/`edit_file` are always bound.
- The `file_path` pydantic field is `str` with no validator; the only constraint is the *description*
  string `"Must be absolute, not relative."` (`deepagents/middleware/filesystem.py:1134`) — a prompt,
  not a gate.
- `filesystem_permissions()` evaluates against the **already-validated** path
  (`_check_fs_permission(self._permissions, "write", validated_path)`, filesystem.py:2051), so it
  *allows* the bypass rather than blocking it. Measured: `_check_fs_permission(rules,"write",
  "/memories/n.md") == "allow"`.
- `CompositeBackend._route_for_path` also runs on the validated path, so routing to `StoreBackend`
  is guaranteed once `validate_path` has canonicalized.

**5. Cleanup.** All probe rows were deleted from the live store afterwards
(`asearch` returns `[]` for both probe namespaces).

### What the reporter missed, and it makes the reachability worse

The finding names only the relative spelling and `/./`. The tool's schema description is the one
thing steering the model toward the canonical form, and it says "absolute". Four bypass spellings
are **absolute and therefore obey it**:

```
'/./memories/n.md'   -> '/memories/n.md'  gated=False  BYPASS
'///memories/n.md'   -> '/memories/n.md'  gated=False  BYPASS
'/.//memories/n.md'  -> '/memories/n.md'  gated=False  BYPASS
'\memories\n.md'     -> '/memories/n.md'  gated=False  BYPASS   (to_posix_path rewrites the seps)
```

(`//memories/n.md` is *not* a bypass — `os.path.normpath` preserves the POSIX double slash, so it
routes to the default `StateBackend`.) The finding's second, "latent" case is also confirmed
exactly as described: `/memories` is `gated=False` but `_check_fs_permission(..., "write",
"/memories") == "deny"`, so that one is blocked today by a different rule.

The finding's proposed fix is correct as written — I checked the edge it depends on:
`validate_path("/memories/")` returns `"/memories"`, which the `resolved == MEMORY_ROOT.rstrip("/")`
clause catches.

### Why the severity is medium and not high

Every factual claim in the finding held under execution, and the reporter disclosed the mitigating
precondition themselves rather than hiding it, so this is not an exaggeration — only a label I
would set one notch lower.

- **On the shipped chart there is no consequence at all today.** `deploy/helm/chemclaw/values.yaml`
  does not set `CHEMCLAW_AGENT_MEMORY_ENABLED`, so it takes its `False` default
  (`core/config/agent.py:101`); `scratchpad_backend` then adds no `/memories/` route and *both*
  spellings land in `StateBackend`, which dies with the checkpoint. The bypass needs an operator to
  turn a documented, default-off feature on.
- **The blast radius, once on, is one user's own agent-authored notes.** `StoreBackend`'s namespace
  is a closure over `memory_namespace(actor)` computed at build time, so there is no cross-user or
  cross-tenant write. Nothing auto-injects `/memories/` into a later prompt — I grepped the agent
  package and the prompts; the only reader is the model's own `ls`/`read_file`.
- **Nothing with real blast radius is affected.** Every tool the dry-run promise and D-167 exist to
  hold back — `compute_dft_energy`, `propose_knowledge_note`, `record_confirmed_answer`,
  `request_development_report`, every connector job, every template launcher — is gated by *name*
  through `side_effecting_tools()`, which no path spelling touches. `writes_durable_memory` is
  consulted for exactly two verbs.
- No safety or impurity-limit answer is involved; a chemist is shown nothing incorrect. The one real
  user-facing harm is that a turn reported as a dry run ("Nothing was started") left a durable row.

I would move this to **high** for any deployment that has actually set
`CHEMCLAW_AGENT_MEMORY_ENABLED=true`, since it is then a live, unauthenticated-by-role,
fail-open bypass of two gates via a spelling a model emits unprompted. The fix is three lines and
should be made regardless — the direction of the failure is the one `authz.py:213`'s own docstring
says must never happen.
