# Verification — `connectors-seam--design.md` (lens: does it actually reproduce?)

In scope: **one** finding. The file has no `critical` findings and exactly one `high`; the other
six are medium/low and were not examined.

Working tree untouched: no source file was edited. The counterfactual below is produced by a
`sys.meta_path` loader that rewrites one line of `calc/remote.py` *in memory* at import, so the
shipped file on disk is byte-identical to `HEAD` throughout. Scripts under `/tmp/vrepro/`.

---

## A connector *server* process loads the agent's authorization module and the whole LangGraph/Temporal client stack, because one bundle imports a private float out of `registry.py`

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. The static claims — all real and current.**

```
$ grep -n "_READ_TIMEOUT_GRACE_SECONDS" src/chemclaw/connectors/registry.py src/chemclaw/connectors/calc/remote.py
registry.py:89:_READ_TIMEOUT_GRACE_SECONDS = 5.0
calc/remote.py:42:from chemclaw.connectors.registry import _READ_TIMEOUT_GRACE_SECONDS
calc/remote.py:127:            sse_read_timeout=timedelta(seconds=bound + _READ_TIMEOUT_GRACE_SECONDS),
```

`jobs.py:37` is `from chemclaw.agent.authz import authorize_trigger, require_actor`;
`identity.py:45` is `from chemclaw.agent.turn_flags import is_dry_run`; `registry.py:45` imports
`jobs`, `:53` imports `transport`; `authz.py:267-270` really does carry the lazy-import comment
quoted ("because the connector registry reaches the agent builder, which reaches this module").

**2. The import chain — re-derived, not read.** I instrumented `builtins.__import__` and walked the
parent map back from the target module (`/tmp/vrepro/why.py`):

```
$ uv run python /tmp/vrepro/why.py chemclaw.connectors.calc.server.app chemclaw.agent.authz
chemclaw.agent.authz <- chemclaw.connectors.jobs <- chemclaw.connectors.registry
    <- chemclaw.connectors.calc.remote <- chemclaw.connectors.calc.compose
```

(The finding writes the middle hop as `...calc -> ...calc.remote`; it is actually
`calc.server.tools -> calc.compose -> calc.remote`. Immaterial.)

**3. The measurement — my own counterfactual, and it matches theirs.** Rather than stub the
registry, I rewrote *only* `remote.py:42` into a local `= 5.0` via an import hook, so every other
byte of shipped source still executes. Three runs each:

```
as shipped   : modules=2318  t=1.96 / 2.04 / 1.98 s   rss=215.8 MB
one line inlined: modules=1472  t=1.35 / 1.30 / 1.31 s   rss=158.0 MB
   -> langgraph / langchain_core / langchain_mcp_adapters / temporalio /
      chemclaw.agent.authz / connectors.jobs / connectors.transport / connectors.registry: ALL ABSENT
```

846 modules and 0.67 s, against the finding's 845 and ~0.7 s. The app object still builds
(`FastAPI`). I also measured what they did not: **+58 MB RSS**. And the finding under-reports the
reach — `bo`'s server has the same at-import leak, via `bo.calculators -> calc.remote`:

```
chemclaw.connectors.bo.server.app    6608 modules  7.36 s  registry/jobs/authz/langgraph/temporalio loaded
chemclaw.connectors.calc.server.app  2318 modules  1.97 s  same
chemclaw.connectors.molfp.server.app 1304 modules  1.14 s  none
chemclaw.connectors.rxnfp.server.app 1359 modules  1.34 s  none
```

`values.yaml:161-224` gives `calc` and `bo` `server: true` with their own Deployments, so both are
real pods paying it.

**4. What kills the headline.** `BearerAuthMiddleware` is added unconditionally by `connector_app`
(`server.py:449`), and `_declared()` → `_declared_bearer_env()` (`server.py:106`) does
`from chemclaw.connectors.registry import discovered` **on the first non-probe request**. So I took
a bundle that is clean at import — `molfp` — and sent it one request (`/tmp/vrepro/firstreq.py`):

```
after import : 1304 modules; none of the heavy set
POST /mcp -> 421
after req    : 2176 modules; ['langgraph', 'langchain_core', 'langchain_mcp_adapters',
                              'temporalio', 'chemclaw.agent.authz', 'chemclaw.connectors.jobs',
                              'chemclaw.connectors.transport', 'chemclaw.connectors.registry']
```

+872 modules, in a process that never touches `remote.py:42`.

**5. Two smaller factual checks.**

- `deploy/entrypoint.sh:63-74` execs `python -m chemclaw.connectors.server_entry "${name}"`, not
  `uvicorn chemclaw.connectors.calc.server.app:app`. `server_entry.py`'s own docstring records that
  the app-object form was removed deliberately. The app module is still imported (uvicorn gets the
  import string), so the trigger holds — but the cited command does not ship.
- The proposed regression test ("assert `chemclaw.agent` is absent from `sys.modules` after
  importing a bundle's `server.app`") fails **today on every bundle**, fix #1 or not:
  `chemclaw.connectors.molfp.server.app -> ['chemclaw.agent', 'chemclaw.agent.turn_flags']`, because
  `server.py` imports `identity.py` which imports `agent.turn_flags` at module scope.

### Why

The mechanism is real, the line numbers and symbols are current, and every number in the finding
reproduces on my own scaffolding within a few percent — 846 modules / 0.67 s / 58 MB per `calc` pod,
and the same leak in `bo`, which the finding misses. Nothing here is fabricated.

What is overstated is the causal claim the finding is titled and priced on: *"bought by one line
that wants a `5.0`."* It is not. The registry — and with it `jobs`, `agent.authz`, `transport`,
`langgraph` and `temporalio` — arrives in **every** connector server process, `molfp` and `rxnfp`
included, the moment the first MCP request hits `BearerAuthMiddleware`. `remote.py:42` changes
*when* the 850-odd modules load in two of the four server bundles (pod start vs first request), not
*whether*. So the finding's fix #1 — move the timeout constants to `core/http.py` — buys almost
nothing on its own: it moves 0.67 s from cold start to the first request and leaves the RSS
identical for any pod that serves traffic. Only fix #2 (breaking `connectors -> agent`, plus the
`server.py:106` registry read) removes the cost, and only the `authz`/`turn_flags` half of that is
actually about the agent — `registry.discovered()` is a legitimate thing a connector server asks
for, and it is heavy for its own reasons.

Severity: the consequence is a one-off startup cost, ~58 MB of RSS, and an import-graph coupling.
No gate is bypassed, no decision changes, no request is answered differently, and `agent.authz` has
no import-time side effects — importing a module that makes authorization decisions is not the same
as making one. That is a cleanup with a real number attached, i.e. medium. `high` would imply
something that should block, and nothing here does.
