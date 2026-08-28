# D-2026-08-28-an-output-schema-describes-what-is-sent-not-what-could-be-received — every connector tool's output schema is re-derived in serialization mode

**Status:** accepted · **Date:** 2026-08-28 · Extends
`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`, which established that what a tool sends is
not the model it declares; this is the half of that sentence the *advertisement* had not learned.

## Context

`campaign_progress`, called through the front door against the running `bo` connector, returned:

```
Output validation error: Additional properties are not allowed ('out_of_space', 'summary' were unexpected)
```

on every call. The tool's return annotation is `chemclaw.science.bo.progress.CampaignProgress`: 16
declared fields, two `@computed_field`s (`out_of_space`, `summary`), and
`model_config = ConfigDict(extra="forbid", frozen=True)`.

## What was measured

`mcp` 1.29.0's `func_metadata._try_create_model_and_schema` builds a tool's output schema with

```python
schema = model.model_json_schema(schema_generator=StrictJsonSchema)
```

and takes pydantic's default, `mode="validation"`. A validation schema omits every computed field by
design — a computed field is an output and can never be an input. The value the same module then
puts on the wire is `FuncMetadata.convert_result`'s
`validated.model_dump(mode="json", by_alias=True)`, which **includes** them:

```
CampaignProgress.model_json_schema()                        -> 16 properties, additionalProperties: false
CampaignProgress.model_json_schema(mode="serialization")    -> 18 properties
the live bo server advertised                               -> 16 properties, additionalProperties: false
```

The refusal is the **server's own**, not the client's, which is worth stating because it changes
where to look: `mcp.server.lowlevel.server`'s `call_tool` handler runs
`jsonschema.validate(instance=maybe_structured_content, schema=tool.outputSchema)` on the way out
and converts a failure into an error result. The connector rejected the result its own tool had just
computed.

**Blast radius, enumerated over every in-tree bundle that ships a server** (`bo`, `calc`, `molfp`,
`rxnfp` — `chem`, `safety` and `results` serve no MCP surface here), by walking each server's tool
manager and diffing the two schema modes rather than by grepping for `computed_field`, because a
computed field on a *nested* model counts too:

- **31 tools served.**
- **15 of them** returned a model carrying a computed field somewhere, and every one advertised a
  schema without it. Four of the fifteen carry one on a nested model as well
  (`predict_outcome` via `FitQuality`/`Prediction`; five `rxnfp` tools via `CorpusCoverage`).
- **1 of the fifteen was actually failing**: `bo.campaign_progress`, the only one whose model sets
  `extra="forbid"` and therefore emits `additionalProperties: false`.
- The other **14** passed only because their schemas permit unknown properties. That is a
  *validator default*, not a promise: the field was undeclared on every one of those calls, and an
  `extra="forbid"` added to any of those models later — or a stricter consumer — converts each into
  the same outage with no code change anywhere near it.

The affected fifteen: `bo.suggest_next_experiment`, `bo.campaign_progress`,
`bo.generate_screening_design`, `bo.predict_outcome`, `calc.calculator_trust`,
`calc.calculator_outliers`, `molfp.similar_molecules`, `molfp.substructure_matches`,
`rxnfp.similar_reactions`, `rxnfp.substrate_precedent`, `rxnfp.conditions_for_similar_product`,
`rxnfp.conditions_for_similar_reaction`, `rxnfp.reagent_frequency`,
`rxnfp.reactions_making_substructure`, `rxnfp.workup_precedent`.

## Decision

`chemclaw.connectors.server.connector_app` re-derives every registered tool's output schema in
**serialization** mode before the app is built, keeping upstream's `StrictJsonSchema` generator:

```python
metadata.output_schema = metadata.output_model.model_json_schema(
    schema_generator=StrictJsonSchema, mode="serialization"
)
```

Serialization mode is not merely a wider schema; it is the schema *of the call that produces the
payload* — `model_dump(mode="json", by_alias=True)`, the same alias handling included. Computed
fields appear, and so does the fact that a serialized model always carries every field, which is why
`required` grows.

### Why at that seam

- **Not in the models.** Dropping `computed_field` for a bare property takes the sentence back out
  of the payload, which is the entire reason those fields exist — `FingerprintSearch.verdict`
  records the argument, and eleven other docstrings in this tree cite it: *a plain property is not serialized,
  so the caveat never reaches the model composing the answer*. A tool docstring is read once, when
  the tool is defined; the computed sentence is in the context window at the moment the answer is
  written, and only one of those two is load-bearing.
- **Not by relaxing `extra="forbid"`.** That guard is about what may be *constructed*; it has
  nothing to do with what is advertised, and relaxing it would leave the other fourteen tools
  advertising a contract they still do not honour.
- **Not per tool.** `connector_app` is the one place every connector's surface is built through —
  the same argument `_sanitize_tool_errors`, `_bind_caller_per_tool_call` and
  `_publish_tool_results` are installed by. A tool author has nothing to remember, and a bundle
  added tomorrow is covered on the day it is added.

### Failing loudly rather than degrading

If pydantic cannot produce a serialization schema for a return type, this raises — at app-build
time, which is import time for a bundle, so the connector refuses to start. That is deliberate and
matches `_declared_bearer_env`: a connector that cannot state what it sends should not serve a
contract already known to be wrong. Measured across all 31 tools, no return type raises today.

## The upstream coupling, and how it retires itself

This is a workaround on a shape `mcp` never promised, so it is pinned in
`tests/test_upstream_surface.py` per `D-2026-08-14-the-coupling-is-the-cost-not-the-line-count` —
in both directions:

- **An absence test.** `test_a_tool_output_schema_is_still_built_in_pydantic_s_validation_mode`
  registers a computed-field model on a bare `FastMCP` (bare, deliberately: an app built through
  `connector_app` has already been repaired, so asking one would assert the fix against itself) and
  asserts the computed field is *missing*. The day `mcp` builds this in serialization mode, that
  test goes red and the workaround is deleted rather than left to outlive its reason.
- **A presence test.** `test_a_tools_output_schema_is_still_a_writable_attribute_of_its_metadata`
  pins the two places the repaired schema is written: `FuncMetadata.output_schema` is assignable,
  and `Tool.output_schema` is a `cached_property` over it whose cache the repair drops — otherwise
  `FastMCP.list_tools` and the lowlevel handler's cached tool definition could disagree, which is
  this same failure inverted. It also pins the skip condition, and correcting *that* took a
  measurement: a `-> str` return does **not** skip, because upstream wraps a primitive in a
  generated model under `result`; only a tool with no return annotation has no output model.

`mcp` also gains a version floor in that file's `test_the_pinned_versions_...`, which it did not
have while three assertions there already read its internals.

## Consequences

- Every connector tool now advertises the fields it sends. `bo.campaign_progress` works.
- Advertised output schemas grow (computed fields, and a fuller `required`). This does not touch the
  per-turn prompt cost measured by `chemclaw_connector_tool_schema_tokens`, which is the *input*
  schema surface.
- Two tests hold the product end of it in `tests/test_connector_transport.py`:
  one drives a real `CampaignProgress` through the real wrapper over a real HTTP transport and
  validates the returned `structuredContent` against the advertised `outputSchema`, and one sweeps
  every tool of every shipped bundle over the wire. Neither compares two `model_json_schema()`
  calls, which agree with each other whatever the server advertises.

## What this is not evidence about

Servers in `Chemclaw3-mcp` are built by that repository's own `connector_app`, not this one, so
nothing here fixes or measures them. Any server there returning a model with a computed field has
the identical defect, and the fleet's `mcp_server_kit` is where it would be fixed.
