# D-2026-09-16-a-truthy-string-is-not-the-flag-somebody-wrote — a manifest's `config:` is checked for what it was given, not only for what the callable accepts

## Status

Accepted. Extends `D-2026-08-26-the-driver-s-signature-is-the-schema` rather than revising it: the
signature is still the schema, and it turns out to have declared types that nothing read.

## Context

A data source's `config:` block is free-form `dict[str, Any]` on purpose — "the callable's own
signature is the schema, and `make datasource-validate` binds these against it, so a wrong key is a
validation failure without a second model to keep in step with the adapter".

Binding is `inspect.signature(...).bind(**options)`, which checks **names**. `signature_mismatch`,
the shared offline half, says so outright: *"Values are irrelevant here and are bound as empty
strings: this asks what the callable accepts."* That is exactly right for a `connection:` block,
whose values are addresses and secrets and where the only question is whether the driver takes the
keyword.

It is wrong for a `config:` block, whose values are **behaviour**. `commitments-json` has one such
key, and it is the most dangerous kind:

> `snapshot` licenses a *destructive* sweep that deletes every commitment absent from the export.

Measured, over `yaml.safe_load` and the real factory:

| written in the manifest | reaches the callable as | destructive sweep |
| --- | --- | --- |
| `snapshot: true` | `True` | armed |
| `snapshot: false` | `False` | off |
| `snapshot: "false"` | `'false'` | **armed** |
| `snapshot: "no"` | `'no'` | **armed** |

A manifest value is passed through exactly as YAML parsed it, and every non-empty string is truthy —
so the quoted spelling of the word that turns the sweep *off* is the spelling that turns it *on*.
The quoted form is not exotic: it is what a person writes when they are unsure whether YAML will
coerce, and what several templating tools emit.

Neither gate could see it. `make datasource-validate` bound the name; `_build_half` caught a
`TypeError` for a name the callable will not take. Both asked what the callable accepts.

## Decision

`chemclaw.core.connect.option_type_mismatch(target, options)` returns an empty string, or one
sentence naming the key, what was written and what the parameter is annotated as. It is called in
**both** places — `_build_half` and `_check_half` — because a check in only one of them is a check
an operator can walk past, and because the gate and the build disagreeing is its own defect.

It judges only the four scalar annotations `_ACCEPTED_SCALARS` names. A union, an `Optional`, a
container or an unannotated parameter is passed over rather than guessed at: a checker that refuses
a correct manifest is worse than the hole it closes. The hole is specific, and the argument is not
symmetric across the four — **every wrong spelling of a `bool` is silently the opposite of what was
written**, which is not true of the other three.

`bool` accepts only `bool`, which is stricter than Python. `snapshot: 0` behaved correctly before
and is now refused. That is deliberate: accepting `0` means accepting a coercion, and once one
coercion is accepted the line between `0` and `"false"` is a matter of taste rather than of type.
The rule is one sentence — **a flag is spelled `true` or `false`** — and the refusal says it. No
shipped manifest uses the integer form; both validators pass unchanged.

## Consequences

- A manifest that wrote a flag as a quoted word now fails `make datasource-validate` with the fix in
  the message, instead of arming a destructive sweep at runtime.
- `signature_mismatch` is unchanged and its docstring is still right about `connection:` blocks. The
  two checks answer different questions and are not merged.
- `extra` kwargs — this repository's own keywords, not a manifest's — are not judged. A defect there
  is a code defect and fails in review.

## What keeps it true

- `tests/test_datasource_seam.py::test_a_quoted_flag_in_a_manifest_is_refused_rather_than_read_as_true`
  drives a real manifest through **both** the build and the gate, so neither can be the only one
  that checks.
- `tests/test_datasource_seam.py::test_a_flag_written_the_way_yaml_spells_one_is_accepted` holds the
  other direction, so "refuses everything" cannot pass as "checks the type".
