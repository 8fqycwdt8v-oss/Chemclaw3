# D-2026-08-28-the-load-1-guard-covered-22-of-99-names — the schema lives in the bundle, and the bundle is in this tree

`chemclaw.cli.mock_llm._validate` is the guard that exists because LOAD-1 published "100 tool calls,
the tool path is genuinely exercised" over calls that all died in the parse-error branch before a
tool body ran. Its own docstring says a behaviour that passes it "cannot fail for the reason every
measurement in the previous load test failed".

It checked the arguments of **22** of the 99 names it accepts, and every one of the 22 was
in-process.

## What was measured

The 2026-08-28 live campaign filed a `BACKLOG` row against this function on the strength of three
failing checks in the storm's `T` family (`t-calc-electronic`, `t-calc-ledger`, `t-bo-inline`),
with a stated mechanism: that `available_tool_names()` and the set the compiled graph binds at turn
time are different sets, so a name can validate and not bind. **That mechanism is refuted**, and
the refutation is the first thing this ADR owes, because the row would have sent the next reader at
the connector registry.

Measured in one process, with every in-tree bundle's MCP server opened over an in-memory session
and its tools loaded exactly as `HeldConnectorSession` loads them (`load_mcp_tools`, then
`transport._allowed` against the manifest's declaration):

```
available_tool_names()          99
advertised_tool_names(None)     91          # the 8 are skills(6) + write_todos + task
in-process bound                45
connector tools bound           31          # bo 5, calc 17, molfp 2, rxnfp 7 — every one declared
accepted by _validate, not executable       15 — all of them chem/safety, whose servers live in
                                                 Chemclaw3-mcp and cannot be opened offline here
executable, not accepted                     0
```

Every manifest-declared connector tool binds. The 99/91 gap is the three middleware name spaces,
which is what `available_tool_names`' docstring says it is. The row's own "reachability probe" —
the one that read the reachable set out of the model's truncated `try one of […]` error string —
had already been retracted in the campaign's README as a check bug; the conclusion drawn from it
had not been.

The three `T` failures are four tools, not eleven: `compute_atomic_descriptors`,
`compute_surface_potential`, `calculator_outliers`, `campaign_progress`. Their behaviours' arguments
are all legal — `campaign_progress` was driven in-process with the storm's own payload and returned
a `CampaignProgress`. Whatever failed there failed in a tool *body* or on the wire to a backend, and
that is a different finding; the `BACKLOG` row is deleted rather than rewritten, because a row is a
claim about the code and this one is wrong about its own mechanism.

## The defect that is real, in the same function

```python
fn = by_name.get(call.tool)
if fn is None:  # an MCP connector tool — its schema lives in the bundle, not in-process
    continue
```

The schema does live in the bundle. **The bundle is in this tree.**
`chemclaw.cli.validate_templates._resolvable_signatures` has resolved exactly those signatures out
of `connectors/<name>/server/tools.py` since the capability migration, and it does so to ask the
*identical* question — does this caller pass arguments the tool takes — about a template step. Two
guards against one failure, one of them answering for a third of the surface, and the half that was
silent is the half a live storm drives.

So a behaviour calling `compute_atomic_descriptors(query=…)`, `calculator_outliers(matchingg=…)`,
`campaign_progress(quer=…)` or `similar_molecules(query=…)` was green-lit at mock startup and would
have died in the parse-error branch before the tool body ran, to be reported afterwards as a tool
call that happened. That is LOAD-1 verbatim, inside the guard written to make LOAD-1 impossible —
and `similar_molecules(query=…)` is LOAD-1's own literal argument name.

## Decision

**`chemclaw.agent.chemclaw_agent.tool_signatures()` is the one answer to "what parameters does the
tool with this name take", and both guards read it.** It sits beside `available_tool_names` for the
reason that function exists at all: several callers were asking one question about one surface.

- `_validate` resolves every call through it. Coverage goes from 22 to **53** of 99 names.
- `validate_templates._resolvable_signatures` is a lazy-import wrapper over it, so
  `make template-validate` keeps its "no agent import at module scope" property and there is one
  implementation rather than two that agree today.
- What stays unresolvable is now *stated* rather than implied by a `continue`, because a silent
  skip is how the first gap survived: a bundle served from `Chemclaw3-mcp` (`chem`, `safety`) ships
  no server module here; a generated launcher takes one `params` object, whose fields
  `tests/test_storm_behaviour_coverage.py` validates against the model `build_job_tool` annotates;
  and the skills, harness and subagent tools are upstream's.

**Not by widening the guard.** Every one of the 57 shipped behaviours still passes — the storm's
arguments were correct, which is why nothing had noticed.

One hazard is retired by the move rather than by a rule. `_resolvable_signatures` read
`registered_tools()`, populated only as an import side effect of `chemclaw.agent.chemclaw_agent`,
from a module that had to remember to import it — measured at 30 signatures against 50 when the
order slipped, with the validator still printing "template validation passed".
`tests/test_templates.py` guarded that with a subprocess probe. Defining the function *in* that
module means there is no way to call it without having imported it; the probe stays, now pinning
the coverage a fresh interpreter reaches rather than an ordering nobody can get wrong.

## What holds it

`tests/test_live_storm.py` gains three, and all three fail against the code this replaces:

- one connector tool with a wrong argument name is refused, with the LOAD-1 wording;
- the ratchet — **every** advertised connector tool whose bundle ships a server module here is
  argument-checked, driven by feeding each one a nonce argument, so a bundle added to this tree
  arrives inside the guard rather than beside it;
- the two guards resolve the same set of names, as an equality rather than a subset, because a
  second implementation that merely agrees today is what this is here to stop.
