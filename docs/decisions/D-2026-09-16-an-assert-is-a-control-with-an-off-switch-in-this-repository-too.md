# D-2026-09-16-an-assert-is-a-control-with-an-off-switch-in-this-repository-too — the rule the sibling has, and the one assert here that needed it

`Chemclaw3-mcp` states a rule its serving code is held to: **no `assert` in serving code**, because
`python -O` deletes every one, so an invariant enforced by an assert is a control conditional on how
somebody started the process. It has a test behind it and an ADR
(`D-2026-09-12-an-assert-is-a-control-with-an-off-switch`). This repository had no equivalent, four
asserts in `src/`, and no way to tell which of them mattered.

## What was found

Three of the four narrow a type for mypy and lose nothing when they vanish — the code after them was
already correct or already broken:

| site | what it narrows |
| --- | --- |
| `science/labels/store.py` | `row.labelled_at is not None` — the caller's query filtered on it |
| `agent/chemclaw_agent.py` | `profile.tool_names is not None` — only reached when a profile narrows |
| `cli/live_data.py` | `dataset.dataset_id is not None` — set by the request that just created it |

The fourth is not like them, and sat among them looking exactly like them:

```python
_TOOL_USAGE_ONE = _TOOL_USAGE.replace(
    "WHERE ts >= %s AND ts < %s", "WHERE ts >= %s AND ts < %s AND tool = %s"
)
assert _TOOL_USAGE_ONE != _TOOL_USAGE, "the predicate this narrowing rewrites has moved"
```

That is the guard on whitespace-sensitive string surgery against a sibling constant, and under `-O`
its absence is **silent** rather than loud: `_TOOL_USAGE_ONE` becomes identical to `_TOOL_USAGE`,
`tool_usage` still appends its third parameter, and psycopg raises a bind-count error at *query* time
— from a route, at runtime, naming neither the constant that moved nor the module it moved in. The
whole value of the check is that it fires at import, and `-O` is exactly when it does not.

## The decision

The guard becomes `if ...: raise RuntimeError(...)`, with the message naming the predicate that has
to occur in `_TOOL_USAGE` for the narrowing to mean anything.

The rule is adopted here **as an allowlist rather than a ban**, because the distinction that matters
— narrowing versus enforcement — is not one a scanner can make. `_NARROWING_ASSERTS` names the three
remaining sites with the reason each belongs there, and the test fails a *fourth* appearing without
an argument. That is the moment to notice the statement should have been `if ...: raise`, and it is
also, deliberately, a moment a reviewer sees: adding a line to that dict is a diff somebody reads.
The list is checked in both directions, so an entry that outlives its assert is a failure too.

## What keeps it true

- `tests/test_repo_map.py::test_no_assert_in_src_enforces_an_invariant`
- `tests/test_operations.py` (the `tool_usage` narrowing still returns one tool's rows)
