# Verdicts — `ingest-warehouse--security.md` (lens: does it actually reproduce?)

Scope: findings marked **critical** or **high** only. The file has exactly one — the warehouse
retrieve half's missing authorization gate. The other four are medium/medium/medium/low and were
not examined.

---

## The warehouse ELN retrieve half has no authorization gate, and its binding cannot declare one

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I did not run the reporter's `/tmp/audit/*` scripts and did not use `tests/warehouse_fake`. I wrote
my own driver fake (`/tmp/vprobe/myfake.py`, ~25 lines: a `placeholder` property, an async `cursor`
context manager, `execute` recording the statement, `fetchall` returning primed dicts) and drove the
real `WarehouseVectorRetriever` against it, naming it from the binding as
`driver: myfake:open_fake`.

**1. The retriever itself, no actor and then a wrong-role actor** (`/tmp/vprobe/probe1.py`):

```
actor: None roles: set()
chunks to unauthenticated turn: 1
   eln-snowflake:RX-1 | PROTOCOL_TEXT: confidential project ORION step 3 / PROJECT_CODE: ORION
actor: oid-outsider roles: {'chemclaw.user'}
chunks to unentitled actor: 1
```

The second half is the part that matters more than the reporter's own transcript, which only showed
the anonymous case: an *authenticated* caller holding a role the source never mentions gets the same
row. There is no identity read anywhere on the path — `grep -rn "get_current_actor\|get_current_roles"
src/chemclaw/ingest/eln/warehouse/` returns nothing (the only hits under `ingest/` are
`documents/retriever.py` and the `sharedrive` manifest).

**2. Through the real tool, not just the class** (`/tmp/vprobe/probe3.py`). I built a manifest folder
`/tmp/vprobe/srcdir/eln-wh/datasource.yaml` declaring only the retrieve half, set
`CHEMCLAW_DATA_SOURCES_DIR` / `CHEMCLAW_DATA_SOURCES=eln-wh`, and called
`chemclaw.agent.research_tools.gather_evidence(query=...)` with no identity bound:

```
active retrieve halves: ['eln-wh']
actor: None
[EvidenceChunk(content='<retrieved-note-...  id="eln-wh:RX-9">\nPROTOCOL_TEXT: confidential project
 ORION step 3\nPROJECT_CODE: ORION\n</retrieved-note-...>', source_note_id='eln-wh:RX-9',
 retriever='eln-wh', score=0.95, ..., source='eln-wh:V_EMBEDDING:RX-9')]
```

So the trigger is reachable through the ordinary evidence path — registry → `sweep_sources` →
retriever — and `sweep_sources` (`retrieval/fanout.py:169`) takes `(name, retriever)`, `query`,
`filters` and nothing identity-shaped, as claimed.

**3. The tool gate above it does not compensate** (`/tmp/vprobe/probe4.py`, with
`CHEMCLAW_ENTRA_REQUIRED=true` plus the audience/tenant/client settings its validator demands):

```
entra_required: True default: allow
authorize_tool(gather_evidence) for non-entitled authenticated user: ALLOWED
```

`tool_authz_default` defaults to `"allow"` (`core/config/entra.py:60`) and `gather_evidence` is in
the read set (`agent/authz.py:140`), so per-tool RBAC is not a second line of defence here unless a
deployment switches the whole surface to allowlist mode.

**4. The configuration escape hatch really is closed** (`/tmp/vprobe/probe2.py`):

```
required_roles REJECTED: invalid warehouse binding: 1 validation error for WarehouseBinding
vector.required_roles   Extra inputs are not permitted [type=extra_forbidden, ...]
```

`VectorBinding` is `extra="forbid"` at `binding.py:444` (class at `binding.py:435`), both line
numbers current.

**5. The contrast is real.** `ingest/documents/retriever.py:116` `_entitled()` returns False when
`get_current_actor() is None` and otherwise requires `get_current_roles() & required`, and
`documents/binding.py:210` refuses a non-`public` share that declares no `required_roles` at all —
so the sibling source in the same seam cannot even be configured into the state the warehouse source
is permanently in. `sources/eln-snowflake/datasource.yaml` connects with `role: CHEMCLAW_READER`, a
service role, which is what makes this a confused deputy rather than merely an ungated read.

### Why

Every element of the claim re-derives from source with my own scaffolding: the code path reads no
identity, the cited symbols and line numbers are current, the trigger is reachable through
`gather_evidence`, the tool-level gate is open for reads by default, and the binding cannot express
an entitlement. The consequence as stated holds.

Two qualifications, neither of which changes the verdict or the severity:

- The *unauthenticated* framing is the weaker half. Through the HTTP front door with
  `entra_required` on, `api/auth.require_principal` 401s a missing token and `api/runner.py:189`
  binds the identity, so a chat turn there does have an actor. The anonymous case is real for the
  CLI, the report workflow and dev mode. The load-bearing half is the one I measured in probe 1's
  second block and probe 4: an *authenticated* caller with no matching entitlement is served, and
  that needs no unauthenticated path to be a confused deputy.
- The shipped default `data_sources` is `graph,eln-json`, so no warehouse source is on out of the
  box. The finding states this in its own trigger, so it is not overstatement — it is what keeps
  this high rather than critical.

One thing the reporter missed that makes it slightly worse: the only per-row narrowing the vector
binding offers is `filter_columns` (`tag`/`since`/`until`), which is driven by the *model's* tool
arguments, not by the turn's roles — so there is not even a degraded row-level control an operator
could repurpose as an entitlement. The `where:` literal is static per manifest and equally
identity-blind. A deployment's only actual mitigation today is a narrower `CHEMCLAW_READER` grant in
the warehouse itself, i.e. all-or-nothing per pod.
