# D-2026-08-28-a-refusal-another-process-wrote-is-not-an-internal-error — A backend's worded refusal must survive the last hop

## Context

`connectors/server.py` patches the FastMCP tool manager so an unexpected exception cannot reach the
model verbatim: `ValueError` — "a deliberately-worded domain message" — passes through, and anything
else is logged and replaced with `an internal error occurred`. That rule is right, and its
pass-through family was one class too narrow.

A connector tool that answers from another process raises `McpRequestRefused`
(`core/mcp_session.invoke`), which is a plain `Exception`. So **every refusal a backend wrote for the
chemist was replaced at the last hop.**

Measured on the live lane, 2026-08-28, driving `[[t-calc-electronic]]` through the front door:

```
compute_atomic_descriptors -> Error executing tool compute_atomic_descriptors: an internal error occurred
```

What the calc backend had actually said, recovered from `.live/connectors.log`:

> atomic polarisabilities, dispersion coefficients and atomic multipoles require the `'xtb'` binary,
> which is not installed in this deployment. Nothing here approximates them: tblite exposes no atomic
> multipoles and no polarisability, so there is no in-process fallback to fall back to. The partial
> charges, bond orders and Fukui indices from `compute_electronic_properties` and
> `predict_site_reactivity` do not need it.

A deployment fact, the reason no fallback exists, and the two tools to use instead — discarded, and
replaced with a sentence nobody can act on. The model's only remaining move is to retry.

## Decision

**A refusal that crossed a process boundary has already passed a sanitizer; sanitizing it again keeps
nothing back and destroys the only actionable thing in it.** `Chemclaw3-mcp`'s `connector_app`
guarantees this on its side — anything that is not a deliberately-worded `ValueError` there is already
replaced by a short `error_id` notice before it is sent. So the text arriving here is either the
server's considered wording or an opaque id, never a traceback, a DSN or a path.

`_worded_refusal` therefore admits `McpRequestRefused` and its message is passed through, logged at
WARNING rather than as an unexpected exception.

**Two things about the shape, and both were found by measurement rather than by reading.**

The refusal is raised inside `calc_session`'s `anyio` task group, so what arrives as `__cause__` is a
nested `ExceptionGroup` whose own `str()` is `unhandled errors in a TaskGroup (1 sub-exception)`. A
type check against the cause can never see the refusal, however wide the accepted family gets — the
groups have to be walked.

And walking the groups was still not enough: **a group's leaf is not the refusal, the refusal is what
caused it.** The innermost member is the exception the `async with` raised while unwinding, with
`McpRequestRefused` on its `__cause__`. The first version of this fix passed its unit test and left
the live lane reporting `an internal error occurred` unchanged, which is how that link was found. The
unwrap follows `__cause__` and `__context__` as well as group membership, bounded at
`_REFUSAL_UNWRAP_DEPTH`.

Three hops each name the tool before saying anything, so the delivered sentence read
`Error executing tool compute_surface_potential: compute_surface_potential failed: Error executing
tool compute_surface_potential: atomic polarisabilities …`. Prefixes that name *this* tool are
dropped; nothing carrying information is trimmed.

## Consequences

The chemist reads what the backend meant them to read. A deployment that cannot do something says so,
names why, and names the alternative — which is the difference between a capability gap and an outage.

The blast radius is every connector tool that answers from a backend, not only `calc`: any refusal any
MCP server in the fleet words for a caller now survives the last hop.

`tests/test_connector_transport.py::test_a_backends_worded_refusal_survives_the_sanitizer` drives all
three shapes — an unwrapped refusal, one inside a task group, and one chained as a cause inside a task
group, which is the shape the live failure had — and asserts the tool is named once.

What is **not** changed: an exception that is not a refusal is still logged and replaced. The rule
this narrows is the family, not the posture.
