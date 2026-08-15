# D-2026-08-15-safety-is-a-tool-not-a-gate — the hazard screen becomes an ordinary MCP capability, and the machinery that made it special is deleted

**Status:** accepted · **Date:** 2026-08-15 · Supersedes D-080's **gate** clause and its eval-metric
clause; leaves D-080's capability and its advisory invariant standing. Applies
`D-2026-08-15-capability-moves-judgment-and-declaration-stay`.

## Context

Scientific capability is moving to [`Chemclaw3-mcp`](https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp);
this repository keeps infrastructure. `safety` is the second bundle to go, after `chem`, and the
first where moving the *engine* is the smaller half of the change.

The user's framing decided it: **safety is like GxP** — still important, no longer a structural
concern of this repository, and it "should be handled like any other MCP tool". That is a statement
about architecture, not about chemistry, and it is the same move
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` made for GxP,
with the same shape: the framing goes, the machinery that existed **only to serve the framing** goes
with it, and the capability survives untouched.

D-080 gave the hazard screen four things no other capability in this tree has ever had.

## Decision

**Safety is a tool. It gets what a tool gets and nothing more.**

The three tools — `screen_hazards`, `screen_genotoxic_alerts`, `ich_impurity_limit` — are served by
`Chemclaw3-mcp:servers/safety`, on port 8859, behind the same bearer every other server there
enforces. `connectors/safety/connector.yaml` stays as the declaration and
`connectors/safety/skills/safety-screening/SKILL.md` stays as the judgment, exactly as the preceding
ADR requires: **capability moves, judgment and declaration stay.**

## What is retired, and what it cost

Four mechanisms, each of which existed because the screen was privileged:

| retired | what it did | why it goes |
|---|---|---|
| the `kg-validate` hazard gate | an agent-authored `## Procedure` note whose structures tripped the table at ≥ `safety_gate_severity` had to carry a `## Hazards` section or the PR failed | it called a screen that no longer lives here, and a *gate* is exactly the privilege being withdrawn |
| `hazard_flag_recall`, floor 1.0 | scored the pinned rules that must still fire, so a silently-broken SMARTS failed `make eval` | it screened a corpus that left with the engine; the property is the server's to guard now |
| `make safety-validate` + its CI step | force-compiled both SMARTS tables so a bad row failed at build | the tables are baked into the server's image and compiled by that repository's own tests |
| `core/config/safety.py` — all four settings | `safety_rules_path`, `safety_gate_severity`, `safety_gate_enabled`, `safety_max_components` | the first three configured the gate; the fourth moves to the server, which is where the amplification it bounds now happens |

Also gone: `science/safety/` (including `notes.py`, whose only caller was the gate), the bundle's
`server/`, `cli/validate_safety.py`, the two eval cases pinning rule ids, `tests/test_safety.py`,
`test_safety_pairs.py`, `test_validate_safety.py`, and the `DEFERRED.md` row deferring hazard
screening *beyond* structural alerts — that question is no longer this repository's to defer, and it
follows the capability.

**The reduction, stated rather than discovered: CI no longer screens a proposed procedure on a
reviewer's behalf.** Before this change, an agent-proposed procedure containing an organic azide
could not merge without a `## Hazards` section; now it can. What still stands: the agent is told to
screen before proposing chemistry, the skill holds how to act on a flag, the three tools are
`read_only` so they work under an unapproved plan, and a human reviews every note at the PR-gate.
That last one is not a weaker version of the gate — it is what a PR-gate always meant, and the
automatic check was the addition, not the baseline.

**The invariant survives, and it was never the gate.** *The system flags, it never certifies.* It
lives in the tool docstrings, in `ScreenResult.verdict` as a serialized `computed_field`, and in the
test asserting no clearance-like phrasing can appear — all three ported verbatim to the server,
because an over-trusted screen is more dangerous than none.

## Why this is not `reject_widening` in reverse

`D-2026-08-15` deleted `reject_widening` on the grounds that **a guard with no caller, kept alive by
a test that calls it directly, is a claim that a control exists.** The symmetric mistake here would
be keeping the hazard gate as a `kg-validate` branch that can no longer reach a screen, or keeping
`safety_gate_enabled` as a setting nothing reads. Both would read as live enforcement. Deleting them
is the same rule applied to the same failure mode; what differs is only that here the control did
work, and is being withdrawn deliberately rather than found dead.

The one thing that emphatically stays is `tests/test_connector_safety_rubric.py`, whose name is
misleading: it is not about the hazard corpus. It proves that any connector tool is audited and
role-gated identically to an in-process one — which is precisely what underwrites moving capability
out of process at all.

## Prose that claimed a control which did not exist

`data/profiles/safety.yaml` opened by saying safety "is not attenuable away … refused at build time
(`chemclaw.agent.team`)". That module was deleted with the specialist team and there has been no
such refusal since. It is removed here rather than carried, on the same grounds: a sentence
asserting a control that does not run is worse than no sentence, and this file is read by whoever
next asks what protects the screen.

Two tests also lost their premise. `tests/test_live_probes.py` and `tests/test_runner.py` built a
realistic tool result by calling `ich_impurity_limit`'s own implementation, arguing that "a
transcribed table can drift, and a check that passes against a stale copy of the evidence proves
nothing about the live one". With the table gone there is no live copy in this checkout to drift
from, so the payload is recorded verbatim in `tests/recorded_tool_results.py` and the module says
why that is now the honest form. Neither test is about ICH: they assert that the trace reads figures
past the audit preview, and that an answer quoting them scores as grounded.

## Consequences

- An operator gains two obligations the chart cannot discharge, the same pair `chem` introduced: the
  server's host in `networkPolicy.egressDestinations`, and `CHEMCLAW_SAFETY_TOKEN`. A missing
  credential is a **refused** call, not an open one.
- The tool list exists in two repositories with nothing structurally forcing agreement. The server's
  copy is authoritative; `make connector-validate` against a running server is the check that
  catches a drift. Same accepted cost as `chem`, stated again because it doubled.
- `make ci` runs eight validators, not nine.
- `ARCHITECTURE.md`'s "three defaults resolve against the installed package" is now two.
