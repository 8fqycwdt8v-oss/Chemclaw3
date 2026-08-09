# D-2026-08-08-a-private-import-of-a-type-alias-is-not-a-dependency — a private import of a type alias is not a dependency

**Status:** accepted

## Context

The 2026-08-08 hardening campaign carried an open item from its enforcement lane: *pin
`agent-framework-core<1.12` and funnel the private-module imports through one shim.*

The concern behind it is sound. `agent-framework-core` is required as `>=1.11.0` with **no upper
bound**, and `chemclaw.agent.loop_cap` and `chemclaw.agent.plan_gate` both imported
`ShouldContinueCallable` and `ShouldContinueResult` from `agent_framework._harness._loop` — a
private module. A patch release that moves either name is an `ImportError` at **process start of
both the front door and the worker**, which is the worst possible place to learn about a
dependency's refactor. `tests/test_third_party_layering.py` (D-2026-08-08-a-rule-with-no-test-is-a-claim)
pinned the two sites by `(file, symbol)` so a third could not appear quietly, but that is
containment, not a fix.

The review that produced the item counted **five** private imports. Three were already dissolved by
asking the installed package rather than the comments beside them: `todos_remaining`, the
agent-mode trio and all five `_compaction` names are exported at `agent_framework`'s top level, are
listed in its `__all__`, and are the *identical objects* — so those imports became public ones.

That left two, and asking what they *are* dissolved the rest of the item. Measured against the
installed 1.11.0:

```
ShouldContinueResult   = 'bool | tuple[bool, str | None]'
ShouldContinueCallable = collections.abc.Callable[..., 'ShouldContinueResult | Awaitable[ShouldContinueResult]']
```

They are **type aliases and nothing else**. No class, no runtime behaviour, no identity that
anything compares against; `ShouldContinueResult` is literally a string. MAF accepts the predicate
structurally — it calls the callable with keyword arguments of its choosing and awaits whatever
comes back. So the import bought exactly two things: type annotations, and a startup-time failure
mode.

## Decision

**Declare both aliases locally, in `chemclaw.agent.harness_types`, and pin nothing.**

Same annotations, no import of a private module, and nothing for a version bound to defend. Both
`loop_cap` and `plan_gate` import from the new module; `_KNOWN_PRIVATE_IMPORTS` is now empty.

### Why not the pin

A pin would freeze the package's security updates — in a repository that has just wired
`deps-audit` into `make ci` precisely so a stale dependency fails the build — in order to defend
against a module that nothing imports any more. And it would still say nothing about the failure it
was aimed at: a *shape* change inside 1.11 is invisible to a version bound.

### The cost, and the test that removes it

Declaring your own copy of somebody else's type trades a loud failure for a silent one. A type
alias is erased at runtime, so if MAF changed the predicate's shape, `mypy` would go on cheerfully
checking our code against our own stale copy and nothing would say a word.

`tests/test_harness_types.py` is the replacement signal. It reads MAF's private definitions **in a
test** — where an `ImportError` is a skipped test rather than a dead pod — and fails when the two
have drifted apart, naming what changed. A third test forbids the private import returning, parsed
with `ast` rather than grepped, because `harness_types`'s own docstring names the module it
replaced and a substring search counted that as the thing it forbids.

So the signal does not disappear; it moves off the startup path and into CI. That is the whole
trade.

## Consequences

- `agent-framework-core` stays unbounded, and is now *less* exposed than before: five imports that
  depended on private module paths depend on `__all__` membership or on nothing at all.
- `_KNOWN_PRIVATE_IMPORTS` is empty and kept. Its ratchet is what forced this cleanup to finish —
  `test_no_declared_private_import_is_stale` failed the moment the imports went away, so the rows
  could not sit there re-blessing an import that no longer exists.
- If MAF ever moves or deletes `_harness._loop` entirely, `tests/test_harness_types.py` skips
  rather than fails, and production needs no change. That is the intended outcome, and it is why
  the skip carries a reason saying so.
- A future author who wants the real MAF alias back must delete `harness_types` *and* a test whose
  name says why not.

## Alternatives rejected

**Pin `<1.12`.** Defends against a module nothing imports, at the cost of the dependency's security
updates, and blind to a shape change within the pinned minor.

**A shim module that re-exports the private names.** One import site instead of two, and the same
`ImportError` at process start — the failure mode is the import itself, not its arity.

**A `try/except ImportError` fallback around the private import.** Silently degrades to an untyped
predicate on the release where it matters, which is the shape of failure this repository's
"fail closed" rule exists to forbid.
