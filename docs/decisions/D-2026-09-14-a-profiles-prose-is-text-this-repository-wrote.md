# D-2026-09-14-a-profiles-prose-is-text-this-repository-wrote — the narrowing exemption for a site's prompt was applied to the six prompts this tree ships, and one of them promised a tool it does not bind

## Status

Accepted.

## Context

`PromptBlock` exists because prose promising a tool the graph does not bind is a promise the model
acts on. The default prompt is therefore cut into 31 blocks, each declaring `requires` /
`absent_unless`, and `instructions_for` drops the blocks whose tools this deployment lacks.

A profile that supplies its own `instructions:` skips all of that, and `instructions_for`'s docstring
gives the reason:

> a profile that supplies its own `instructions:` is text this repository did not write and cannot
> cut into blocks, so it is passed through whole and a site that narrows a profile's tools is
> answerable for its own prompt.

That argument is sound for a site's manifest. It does not cover `data/profiles/*.yaml`, which **is**
text this repository wrote — six files, shipped, reviewed here, and checked by nothing.

Measured: `evidence.yaml` tells the evidence specialist that "a spectrum is the band list
`compute_thermochemistry` returned". `evidence` binds fourteen tools and that is not one of them. So
the specialist whose brief opens "never to compute a new value, propose a note, or start a job" had
a calculation tool named in its own system prompt — exactly the defect the blocks were introduced to
end, surviving one function along, on the path the fix did not reach. The same sweep cleared the
other five: `structure_id` and `artifact_refs` also appear in that file and are a model property and
a note field, not tools.

## Decision

Two things, and deliberately not a third.

**The prose is corrected.** The sentence now points at `find_calculations`, which `evidence` does
bind and which is where that band list actually is, and says "neither something you run".

**A guard over the profiles this repository ships**
(`tests/test_profile_discovery.py::test_no_shipped_profiles_prose_names_a_tool_that_profile_does_not_bind`).
Two choices in it are load-bearing:

- **It checks the *declaration*, not the advertised surface.** A connector that is unreachable in
  the current environment drops its tools from what a profile advertises — measured, `computation`
  advertises 20 of the 41 names it declares with no fleet running — so a check against the live
  surface would fail on a laptop and pass in a pod. A repository-owned guard has to be
  environment-independent.
- **The tool universe is the union of every profile's declared names plus the in-process registry.**
  That is what separates a tool name from an argument name in the same prose. Driven: with the
  profile union removed from the universe and the bad sentence restored, the guard passes — so the
  union is the half that catches a *connector* tool, which is what this defect was.

**And the general fix is not built.** A profile supplying *blocks* — `text` plus `requires` — so a
site's own prose is narrowed the way the default's is, is the fix for the case the exemption really
covers. It has no caller: all six shipped profiles are strings, and a second-domain deployment is
hypothetical. It is a `BACKLOG.md` row with this measurement behind it rather than an abstraction
built for nobody.

## The guard's own first draft could not fail, and that is the finding worth carrying

It iterated `load_profiles()`, which returns **what it newly registered, not what exists** — it is
idempotent by skipping names already in the registry, so the second call in a process returns `[]`.
The test therefore looped over nothing and passed green over a defect measured minutes earlier in a
plain interpreter. Nobody misread that contract; it was assumed. The guard now reads the registry,
and `_shipped_profiles` carries the trap in its docstring so the next reader does not re-derive it.

This is the third time this month a guard written for a real defect did not fire on it — the
`x-injected` header assertion that ASGI makes vacuous, the digest notice asserted by a word rather
than by a line, and this. All three were caught by driving the mutation rather than by reading the
test.

## Consequences

- The evidence specialist's prompt no longer names a tool it cannot call.
- A seventh profile whose prose drifts from its `tool_names` fails in CI.
- A site's own profile is still unchecked, which is stated in the test's docstring rather than left
  to be inferred from its scope.

## What keeps it true

- `tests/test_profile_discovery.py::test_no_shipped_profiles_prose_names_a_tool_that_profile_does_not_bind`.
