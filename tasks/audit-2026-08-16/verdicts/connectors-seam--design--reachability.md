# Verdicts — connectors seam (design), reachability lens

Scope: only findings marked **critical** or **high** in
`tasks/audit-2026-08-16/findings/round1/connectors-seam--design.md`.
That file contains exactly one: the first. The remaining six are medium/low and were not examined.

---

## A connector *server* process loads the agent's authorization module and the whole LangGraph/Temporal client stack, because one bundle imports a private float out of `registry.py`

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. Reproduced the shipped import closure.** `/tmp/imp_probe.py`, `uv run python`:

```
modules: 2318 time: 2.05s
LOADED   langgraph
LOADED   langchain_core
LOADED   langchain_mcp_adapters
LOADED   temporalio
LOADED   chemclaw.agent.authz
LOADED   chemclaw.connectors.jobs
LOADED   chemclaw.connectors.transport
LOADED   chemclaw.connectors.registry
absent   chemclaw.agent.langgraph_agent
LOADED   chemclaw.agent.turn_flags
```

**2. Reproduced the counterfactual.** `/tmp/stub_test.py` installs a `chemclaw.connectors.registry`
stub carrying only `_READ_TIMEOUT_GRACE_SECONDS = 5.0` before importing the app:

```
STUBBED modules: 1473 time 1.36s
absent langgraph / langchain_core / langchain_mcp_adapters / temporalio
absent chemclaw.agent.authz / chemclaw.connectors.jobs / chemclaw.connectors.transport
```

845 modules and ~0.7 s of delta — the reporter's numbers to the module.

**3. Proved the attribution rather than assuming it.** `/tmp/trace_imp.py` wraps
`builtins.__import__` and records the importing module for each edge:

```
chemclaw.connectors.registry <- importers: ['chemclaw.connectors.calc.remote']
chemclaw.connectors.jobs     <- importers: ['chemclaw.connectors.registry']
chemclaw.connectors.transport<- importers: ['chemclaw.connectors.registry']
chemclaw.agent.authz         <- importers: ['chemclaw.connectors.jobs']
langgraph                    <- importers: ['langchain_mcp_adapters.interceptors']
```

Exactly one importer of `registry` in the whole closure, and it is the cited line. The chain is as
stated. I checked the alternative path too — `connectors/server.py` (imported directly by `app.py`)
reaches `chemclaw.agent.turn_flags` via `identity.py:45`, but `chemclaw/agent/__init__.py` is a
docstring and `turn_flags.py` imports only `contextvars`, so that edge is free and is not what pulls
the stack in.

**4. Checked what upstream stands in the way of the trigger.** Nothing. `deploy/entrypoint.sh`
`connector-*` runs `python -m chemclaw.connectors.server_entry <name>`, whose `main` calls
`uvicorn.run("chemclaw.connectors.<name>.server.app:app", ...)` — the same import target. There is
no guard, no `sys.modules` assertion anywhere in `tests/` (`grep -rn "not in sys.modules" tests/`
is empty), and `tests/test_layering.py` explicitly *permits* the edge (below).

**5. Checked for side effects, i.e. whether "loaded" means anything happens.** AST walk over the
module-level statements of `registry.py`, `jobs.py`, `transport.py`, `agent/authz.py`,
`durable/publish.py`, `durable/orchestrator.py`:

```
registry.py  -> ['logging.getLogger(__name__)']
jobs.py      -> []
transport.py -> ['logging.getLogger(__name__)']
authz.py     -> [frozenset({...}) x5]
publish.py   -> ['RetryPolicy(...)', 'list(_BAD_DATA_TYPES)']
orchestrator.py -> []
```

No connection, no registration, no I/O, no policy surface instantiated.

**6. Measured the part the reporter missed.** `/tmp/rss.py`, peak RSS after importing the app:

```
SHIP  maxrss MB 215.86
STUB  maxrss MB 157.66
```

~58 MB, against `values.yaml` `resources.connector: requests.memory 256Mi / limits.memory 512Mi`.

**7. Scoped it.** `grep -rn "connectors.registry" src/chemclaw/connectors/*/ --include=*.py`
returns two hits, both in `calc/remote.py` (one import, one comment). Of the four bundles that have
a `server/app.py` (`bo`, `calc`, `molfp`, `rxnfp`), only `calc` is affected.

### Why

Everything measurable in the finding reproduces exactly, and the trigger is a shipped deployment
path (`CHEMCLAW_COMPONENT=connector-calc`) with nothing upstream preventing it. That part is solid
and I would not argue with it. Three things do not hold, and together they cost it the "high" label:

**The consequence is startup cost and coupling, not a behaviour.** There is no failure mode here —
no request is answered differently, no credential is exposed, no availability is affected (0.7 s of
import against a readiness probe with `initialDelaySeconds: 5`). The finding's strongest-sounding
sentence — "`chemclaw.agent.authz` is imported by, and therefore live in, a process that has no
turn, no principal and no authorization decision to make" — is a worse-sounding paraphrase of
"five frozenset literals were evaluated". Nothing in `authz` executes at import, nothing registers,
nothing holds state, and no decision function is reachable from the connector's ASGI app. "Live"
does no work in that sentence except make the reader think about security.

**The edge the fix targets is sanctioned, not an oversight.** The finding's second fix —
"break the `connectors → agent` direction" — treats the cycle as an accident that `authz.py:267`
works around "from one side only". `tests/test_layering.py:274` declares
`("chemclaw.connectors", "chemclaw.agent")` in `_CYCLE_EDGES` with the reason *"connector jobs and
identity plumbing authorize against agent's authz/identity context"*, and the opposite direction
beside it. That is an enforced policy naming this exact edge, so reversing it is a decision to
overturn, not a leak to plug. Fix #1 (move the three timeout constants to a leaf module) stands
entirely on its own — my stub run shows it recovers the whole 845 modules without touching the
`connectors → agent` policy at all, which makes fix #2 unnecessary for the stated cost.

**Two smaller inaccuracies.** The trigger line says `deploy/entrypoint.sh` runs
`uvicorn chemclaw.connectors.calc.server.app:app`; it has not for some time — it runs
`server_entry`, which exists precisely so the app object is not the entrypoint. Immaterial to
reachability (the same string is passed to `uvicorn.run`), but it is the kind of stale detail that
suggests the deployment path was inferred rather than read. And the title generalises to "a
connector *server* process" when one of four such processes is affected.

**What makes it worse than the reporter said,** and the reason this is medium rather than low: the
58 MB of peak RSS is 23% of the connector pod's `requests.memory`, and it is bought for a `5.0`.
Import time on a pod is forgettable; a quarter of the memory request is not.

So: mechanism CONFIRMED and reproduced to the module, reachability CONFIRMED, consequence real but
inflated in kind (a security-flavoured framing over a startup-cost defect) and the proposed
architectural half of the fix argues against an enforced policy it does not cite. Medium.
