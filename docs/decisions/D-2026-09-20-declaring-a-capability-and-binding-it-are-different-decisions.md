# D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions — five process-development bundles, discovered by every checkout and bound by none

**Status:** accepted · **Date:** 2026-09-20 · Supersedes nothing. Answers the condition
`D-2026-09-15-a-capability-in-the-fleet-cannot-refute-a-denial-this-tree-declares-no-bundle-for`
left implicit, and takes the shape of decision
`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not` queued for `rxnpredict`.

## Context

`Chemclaw3-mcp` serves five process-development servers this tree declared no bundle for:
`thermalsafety` (adiabatic rise, MTSR, TMR_ad, the Stoessel class, jacket duty, Semenov, oxygen
balance), `kinetics`, `unitops`, `props` and `suitability` — 33 tools covering most of the
arithmetic a scale-up from bench to kilo lab is made of.

Unreachable, in the only sense that matters. `build_langgraph_agent` binds what
`registry.enabled()` returns, that reads manifests out of `connectors_dir`, and there were no
manifests. Only `infra/live/e2e-full-stack/up.sh` — which mounts the fleet's own `manifests/`
directory — ever bound one, and no chart deployment does.

Two separate costs were being paid for that:

1. **No judgment could be written.** A `SKILL.md` naming `mtsr` fails `make skill-validate`,
   because the validator resolves declared tools against manifests in this tree. So the capability
   that most needs a skill — seven tools that turn calorimetry into a cooling-failure argument —
   was the one capability that could not have one. That is the D-117 defect, and it is the reason
   `connectors/safety/connector.yaml` already stays here for a server this tree does not run.
2. **`D-2026-09-15` had to drop a prompt denial it could not key.** The clause saying a computed
   enthalpy is never "an adiabatic rise, a jacket duty" could not be keyed on
   `adiabatic_temperature_rise`, because `prose-validate` correctly refused a block that would
   drop from no deployment ever. It was deleted instead, under-claiming the limit on purpose.

The obvious fix — declare the five bundles — has a price that four separate entries in
`tests/test_context_floor.py` had already written down, each time declining to pay it. Every bound
tool's schema is serialised ahead of the system message on **every** model call. Measured by that
file against the sibling checkout: `unitops` 7,353 tokens over 7 tools, `suitability` 4,471/7,
`thermalsafety` 3,764/7, `kinetics` 3,389/6, `props` 2,936/6 — **21,913 tokens together**, against
a `SERVED_ELSEWHERE_ALLOWANCE` of 11,000 that covers the three bundles this tree does declare.

`PREFIX_BOUND` is that allowance plus the profile ceiling, and `core/config/agent.py` derives
*both* compaction thresholds from it. So declaring all five in the ordinary way would take roughly
22,000 tokens of thread allowance from every deployment on earth — including every deployment that
never asks a scale-up question — to serve a capability most of them will not enable. That is the
trade the four floor entries kept refusing, and refusing it again would have been right.

## Decision

**Split declaring from binding.** A manifest gains one field:

```yaml
default_enabled: false
```

`registry.enabled()` reads it in exactly one place — when `connectors_enabled` is empty. Silence
then means *every discovered bundle that declares `default_enabled`*, instead of every discovered
bundle. An explicit `connectors_enabled` list is **not** filtered by the flag, and that asymmetry
is the decision rather than an oversight: the flag says what silence means, not what a deployment
may ask for. Filtering the explicit list too would leave an opt-in bundle unreachable by any
configuration, which is `reject_widening`'s shape — a control whose condition cannot occur.

The five bundles are declared here with `default_enabled: false`, each carrying the fleet's own
manifest text (authoritative there, because it is what answers), a `README.md`, and — for four of
them — the judgment that could not previously be written:

| Bundle | Skill | The sentence it exists to make unmissable |
| --- | --- | --- |
| `thermalsafety` | `thermal-safety-assessment` | every input is a measured DSC/ARC/RC1 number, and a GFN2-xTB reaction enthalpy is **not** a process heat load |
| `kinetics` | `kinetics-and-reactor-choice` | this server fits nothing, so it cannot turn a time course into a rate law |
| `unitops` | `unit-operation-sizing` | it holds no data, so it refuses to default the measurement its answer is made of |
| `suitability` | `system-suitability` | a suitability pass is not method validation and not evidence the result is accurate |

`props` gets none: the judgment about *which* solvent is `skills/solvent-selection`'s and already
written. A second skill there would be the same judgment addressed to a different table.

### The validators move to a declared basis, and that is the other half

`enabled()` answers "what can this turn call". Four validators were asking it a different question —
"does this tool exist in this tree" — and getting away with it only because the two sets were
equal. They stop being equal here, so the fork is made explicit:

- `registry.declared_connector_tool_names()` and `chemclaw_agent.declared_tool_names()` — over
  `discovered()`, for `skill-validate`, `prose-validate` and `template-validate`.
- `registry.declared_skills_dirs()` — so an **opt-in bundle's own skill is still validated** on a
  checkout that does not bind it. Without this, four new `SKILL.md` files would ship unread by any
  CI run and free to name a tool their manifest dropped three releases ago: a check whose condition
  never occurs, which is the shape `D-2026-09-15` refused in the prompt and this one refuses in the
  validator.
- `connector_tool_names()` and `skills_dirs()` keep the enabled basis, because the runtime verifier
  and the agent's own skill surface want exactly the old answer. A deployment without
  `thermalsafety` must not be offered judgment about tools it cannot call.

Deletion is still caught in both directions: a tool no manifest declares is absent from the
declared set too.

## Consequences

**`PREFIX_BOUND` does not move, and that is the measurable claim.** `tests/test_context_floor.py`
binds through `enabled()`, so a fresh checkout, `make test` and CI all see the surface they saw
yesterday — 66 bound connector tools, 99 declared. `SERVED_ELSEWHERE_ALLOWANCE` therefore stays
11,000 for a fifth time, and for a fifth time because no chart deployment binds these bundles; what
changed is that now one *can*, by saying so.

**A deployment that wants the capability pays for it, explicitly.** It names the bundles in
`CHEMCLAW_CONNECTORS_ENABLED`, provides each token, and points each URL at the served address. Its
prefix is ~22,000 tokens larger and its thread allowance that much smaller, which is the honest
price and is now a line in a values file rather than a property of the repository.

**`D-2026-09-15`'s dropped denial becomes keyable, and is deliberately not keyed here.** With a
`thermalsafety` bundle in the tree, a `PromptBlock` keyed on `adiabatic_temperature_rise` would
drop in a real deployment, so `prose-validate` would now accept it. That is a prompt change with
its own risk — under-claiming a limit is the safe direction and is what ships today — and it is
left as a separate decision rather than folded into this one.

**The chart gains five entries at `enabled: false`.** `connectorsEnabled` renders only the enabled
names, so an existing release's rendered config is unchanged.

## Alternatives rejected

- **Declare them on by default and raise the allowance.** The honest version of this is "every
  deployment pays ~22,000 tokens of prefix so that some of them can ask about MTSR". Four entries
  in `test_context_floor.py` already declined it on smaller numbers.
- **Leave them fleet-only and mount via `extraConnectors`.** The chart already supports this and it
  is a fine *hosting* story, but it does not put a manifest in this tree, so it closes neither cost
  above: no skill can name these tools, and no validator can resolve them.
- **Filter the explicit enable-list by `default_enabled` too.** Symmetrical, and makes the bundles
  unreachable by any configuration.
- **A second setting (`connectors_also_enabled`).** A second enablement mechanism to avoid one
  manifest field. `connectors_enabled` stays the single switch.

## Revisit when

A deployment enables all five and measures that the prefix cost is not worth the capability — or
the opposite, that enough deployments enable them that the default is wrong. The file that would
show the first is `tests/test_context_floor.py`'s per-profile report; the second is a values file
in more than one release naming the same five bundles, at which point `default_enabled` should be
flipped for the ones that are effectively universal and the allowance raised deliberately.
