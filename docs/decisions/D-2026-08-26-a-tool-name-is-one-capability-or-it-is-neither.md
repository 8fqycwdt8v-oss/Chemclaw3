# D-2026-08-26-a-tool-name-is-one-capability-or-it-is-neither — one declared name is one capability across the enabled set, whichever half of a bundle declares it

## Status

Accepted.

## Context

`props` served an MCP tool called `compare_solvents`: a read of the vendored solvent table, six
tabulated columns per name, microseconds. `calc` declares a durable job called `compare_solvents`:
the same reaction optimized in each solvent, ranked by ΔG, minutes of semiempirical calculation per
species per solvent. Two capabilities, one name, and no relationship between them beyond the
English word.

They are only both present when a deployment puts `Chemclaw3-mcp`'s `manifests/` on
`CHEMCLAW_CONNECTORS_DIR` — which is the wiring `D-2026-08-09-a-connector-we-do-not-run` designed
and `Chemclaw3-mcp/CLAUDE.md` states as the target for every server in that fleet. So this is the
intended configuration rather than an unlikely one.

Measured, with `calc` and `props` enabled against the real manifests:

```
endpoint tools: 21   jobs: 9
COLLISIONS: ['compare_solvents']
union: 29   naive sum: 30
```

Across the whole enabled fleet — `bo calc chem molfp props pyexec qm results rxnfp rxnlabel
rxnpredict safety` — 72 declared names, 71 distinct. Exactly one collision, and nothing raised.

**Why nothing raised.** `registry.job_tools()` refuses two connectors claiming one *job* name, and
its docstring gives the right reason: the name is the authorization key, so a collision makes one
connector's gate apply to another's work. But the check only ever compared jobs with jobs. No check
compared a job name against an endpoint tool name, `connector_tool_names()` unions the two halves
into a `set`, and `agent.chemclaw_agent._narrow` builds `{advertised_name: tool}`. Three places
that each collapse a duplicate silently, and one of them decides what the model can call.

The consequences are not symmetric with "the agent gets the wrong tool", which would be bad enough:

- **`state_changing_tool_names()`** unions both halves too. `props.compare_solvents` is `read_only`
  and `calc.compare_solvents` is a job, and a job is state-changing by construction — so the name
  is state-changing whichever tool the model actually reaches, and the plan gate's answer stops
  describing the thing that runs.
- **`find_job()`** resolves the name to the `calc` job regardless, because it walks jobs only. A
  template step and an agent tool call for one name could reach different code.
- **The loser is not an error.** It is simply absent from the agent's surface, which reads as a
  broken capability rather than as a misconfiguration, in whichever repository the reader opens
  first.

## Decision

**One declared name is one capability across the enabled set, whichever half of a bundle declares
it, and the registry refuses a deployment where that is not true.**

`registry._declared_tool_names()` walks every enabled manifest's endpoint tools *and* jobs, and
raises `ConnectorError` naming both claimants and what each declares the name as. `job_tools()`
calls it and no longer needs its own dict-keyed dedup; `make connector-validate` inherits it,
because `validate_connectors` already calls `job_tools()` for the old job-vs-job rule. Three
pairings are now refused where one was: job/job, job/tool, and tool/tool — the last also unchecked
before, and one `CHEMCLAW_CONNECTOR_URLS` entry away from mattering.

**`props`' tool is the one that renames**, to `compare_solvent_properties`
(`Chemclaw3-mcp`, same branch). `calc.compare_solvents` is named by string in
`skills/solvent-selection/SKILL.md`, `connectors/calc/skills/calculation-selection/SKILL.md`, the
agent's system prompt, `durable/connector_job.py` and two live-test probes; the `props` name had six
references, all inside its own repository. The rename is also the more accurate name: that tool
compares *tabulated properties*, and the docstring now says so and points at the computed question.

Merging them was never on the table. One is a table lookup with no opinion about chemistry; the
other runs a calculation per species per solvent as durable work.

## Consequences

- A deployment that would have served one capability under another's name now fails to start, with
  a message naming both. That is the intended trade: the previous behaviour was a capability
  silently missing from the agent's surface.
- The guard is a property of the *enabled* set, so a fleet may still ship two bundles that collide
  as long as no deployment turns both on. `connector-validate` checks the configured set, which is
  where the question is actually answerable.
- This says nothing about two bundles offering *overlapping* capability under different names —
  that is `Chemclaw3-mcp/CLAUDE.md`'s "never duplicate a Chemclaw3 capability", a different rule
  with a different remedy, and one that must still be argued in a server's README.

## Notes

The rule generalises past this instance, which is why it is stated as a property of names rather
than as a fix to `job_tools`. Both repositories grow tool surfaces independently, neither imports
the other, and the only thing that makes a name mean one thing is a check over the set a deployment
actually enables. There was no such check; there was a check over a subset of it that read like
one.
