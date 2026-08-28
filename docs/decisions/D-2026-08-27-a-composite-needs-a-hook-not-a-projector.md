# D-2026-08-27-a-composite-needs-a-hook-not-a-projector — a tool composite publishes from the tool boundary, and a Hessian row publishes what it actually holds

## Status

Accepted. Extends `D-2026-08-25-a-cache-is-not-a-record` (the seam) and
`D-2026-08-26-a-route-is-not-a-shape` (what routes a composite). Both stand.

## Context

`docs/planning/BACKLOG.md` carried one row naming two gaps and asserting they were one question:

> A Hessian is cached and never published, and neither is the thermochemistry built from it …
> `xtb.hess` is a `calc_type` the server stamps and `_CALC_TYPE_PROJECTORS` has no prefix for it, so
> vibrational frequencies never reach a results store; `ThermochemistryResult` … has a projector but
> **no hook at all**.

Both halves were true. What the row got wrong is *why* they are one question, and the correction is
the useful part of this ADR.

### The measurement that reframed it

The row assumed a prefix for `xtb.hess` would put frequencies in a results store. It cannot, and no
arrangement of that projector could:

- a wavenumber is an eigenvalue of the **mass-weighted** Hessian;
- `HessianPayload` carries `hessian_npy`, `atom_count`, `method`, `solvent`,
  `electronic_energy_hartree`, `max_gradient_hartree_per_angstrom` and a `structure_id` — and **no
  elements**. The masses are in the geometry, which reaches the row as an address;
- a projector is pure and synchronous by construction (the backfill runs the same function over rows
  a different calculator version wrote), so it cannot resolve that address.

So the frequencies exist in exactly one shape in this system — `ThermochemistryResult`, where
`science/calc/thermo.py` has already applied the masses — and that shape reaches **no** hook. The
two halves are one question because *the primitive cannot carry the science and the composite has
nowhere to hand it to*, not because a prefix was missing.

### The third kind of result

Two hooks reach `enqueue_payload`, and between them they were believed to cover everything:

| hook | fires on | routes by |
| --- | --- | --- |
| `science/calc/store.py::publish_stored_result` | a cache **miss** | the `calc_type` the calculation server stamped |
| `ConnectorJobWorkflow._publish_result` (`durable/connector_job.py`) | a finished Temporal **job** | the `payload_kind` on the envelope |

A **tool composite** is neither. `compute_thermochemistry` assembles relax → Hessian → RRHO in one
conversation turn, from parts that are each separately keyed; it has no cache row because its key
would name the geometry its refinement loop settles on (D-011,
`D-2026-08-16-the-physics-leaves-the-cache-stays`), and it is not a job, so no envelope names its
shape. Its projector has been registered since the seam shipped and has **never been called in
production**.

It is not alone, and that is what turns a special case into a seam: `predict_logd` is composed
client-side from a cached remote pKa plus a local Crippen sum, `connectors/calc/remote.py` records
that the calculation server refuses to key it, and `_CALC_TYPE_PROJECTORS` deliberately has no
`logd` prefix because logD has never had a cache row at all. `LogdResult` had a projector and no
caller for exactly the same reason, and nothing had noticed.

## Decision

### 1. The third hook is on the tool boundary, installed once

`chemclaw/publish/hooks.py::publish_tool_result` is offered every tool result, and
`connectors/server.py::_publish_tool_results` is what offers it — the same shared choke point
`_sanitize_tool_errors` and `_bind_caller_per_tool_call` already patch, for the reason that file
already gives: "patched once here, the one shared choke point every connector's app is built
through, rather than once per bundle."

**It cannot be forgotten by a new tool because a new tool does not touch it.** A tool is registered
with `@server.tool()` and is therefore already inside `ToolManager`; nothing is added to a tool
body, and no author has a call to remember. That is the requirement, stated as the failure it
avoids: `audit_events.agent` was a claim that something was recorded with no producer anywhere in
`src/` (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`), and a
`publish_composite()` each tool must call is that shape waiting to happen.

**It wraps `Tool.fn`, not `ToolManager.call_tool`**, and that is measured rather than stylistic.
`FastMCP.call_tool` delegates with `convert_result=True`, so by the time a call returns from the
manager the model has been through `FuncMetadata.convert_result` and is content blocks plus a
structured dict — a tool result is not a model on the wire
(`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`). The hook routes on the model's own name,
and `Tool.fn` is the last point at which it still is one. It also receives the *validated* keyword
arguments, which is what the identity below is derived from.

### 2. A tool composite's identity is the request that produced it

`calc_ref = f"{connector}.{tool}#{stable_hash(arguments)}"`, `calc_type` the route, and the shape
carried beside it as `payload_kind` — D-2026-08-26's rule, unchanged. This is the job hook's own
answer without a workflow: that hook uses the workflow id "because a composite has no cache key: its
identity is the run … the workflow id is itself derived deterministically from the job and its
arguments."

So the same question asked twice collapses to one row on the outbox's `ON CONFLICT DO NOTHING`,
while the same molecule at a second temperature is a second record — which is right, because it is a
second measurement.

### 3. What publishes from there is declared, and the suite derives the declaration

Most shapes crossing the tool boundary are *already* published by the cache hook under their own
cache key; republishing them there would mint a second record of one calculation under a second
identity. So `publish/hooks.py::TOOL_COMPOSITES` names what the tool hook publishes —
`ThermochemistryResult` and `LogdResult`.

A declaration is exactly the thing that rots, so it is not trusted:
`test_every_projector_is_claimed_by_exactly_one_hook` derives it — every projector reachable from no
`_CALC_TYPE_PROJECTORS` prefix and carried by no `XtbJobResult` member is a tool composite by
definition — and fails if the two disagree. A new tool composite therefore fails the suite until it
is declared, which is the discipline `_NOT_YET_PUBLISHED` and `_PRIMITIVES_NOT_PUBLISHED` already
carry one level down.

(The derivation is written against `_CALC_TYPE_PROJECTORS` rather than against
`tests/calc_server_fake.py::_KEYED`, because the fake does not declare `compute_atomic_descriptors`
or `compute_surface_potential` — two tools that do go through `cached_remote` — and deriving from it
would misclassify two cached primitives as composites. That gap in the fake is noted below.)

### 4. `xtb.hess` publishes what its row actually holds

A `_hessian` projector, registered under both the prefix and the model name: the electronic energy
the SCF settled at, the atom count, and `max_gradient` — the evidence that the geometry
differentiated was a stationary point at all
(`D-2026-08-27-a-gradient-is-the-evidence-a-frequency-set-cannot-carry`). No frequency, for the
reason above. `_thermochemistry` publishes the same gradient beside its modes, so the fact that
decides whether a frequency set means anything travels with the frequency set.

**The packed arrays do not ride along.** Every other projector leaves `ResultRecord.payload`
untouched, because that is what makes a projection safe to be wrong — every fact can be rebuilt by
re-projecting. Here the payload is the one in this system that is bytes rather than numbers, and
re-projecting it could not recover a frequency in any case. Measured on the JSON document: **9,217
bytes → 184 at nine atoms, 108,290 → 185 at 33, 1,394,499 → 186 at 120.** `result_publications` is a
queue nobody prunes, and one document is written *per enabled sink*. The matrix is not lost:
`ArrayOffloadingStore` had already put it in the content-addressed artifact store before the row was
written. So an array reaches the record the way a geometry does — by not being copied
(`D-2026-08-21-a-geometry-is-an-address-not-a-payload`).

`project()` states the narrowing rule rather than leaving it implicit: a projector may remove a
payload field that is *bytes rather than science*, and nothing else.

## The defect this found on the way

`_optimization` published `max_gradient` as `"hartree/bohr"`. `OptimizationResult.max_gradient` is
**Hartree/Angstrom** — its own comment says so, and so does
`HessianPayload.max_gradient_hartree_per_angstrom`. The registry keeps `max_gradient` in Hartree/bohr,
so `to_canonical` saw the canonical unit, returned the number unchanged, and **every gradient this
system has published was 1.89x too large with a correct-looking unit string beside it**.

`_fact`'s own docstring had predicted this in as many words — "every call site below passes a unit
that already *is* the property's canonical unit, so the conversion is an identity on every live path
… the first projector reporting an energy difference in hartree or kJ/mol … lands off by 627.5 or
4.184 with the unit string beside it still right". The first such projector turned out to be a
gradient rather than an energy, and it was already merged. Fixed by reporting the calculator's own
unit and adding the one conversion (`hartree/angstrom → hartree/bohr`, the bohr radius) to
`UNIT_CONVERSIONS`; `reported_value` was already carrying what the calculator said, which is what
makes the historical rows recoverable.

## Consequences

- Frequencies, ZPE, the RRHO corrections and their standard state reach a results store, from a real
  `compute_thermochemistry` call, for the first time. So does every logD.
- `_PRIMITIVES_NOT_PUBLISHED` is **empty** and kept, per that set's own rule.
- Every `max_gradient` in a results store is now in the unit the column claims. Rows written before
  this are wrong by 1/0.529177 and are re-projectable from `reported_value` or by re-running the
  backfill.
- A third patch of an upstream FastMCP internal (`Tool.fn` via the public `ToolManager.list_tools`),
  beside the two already in `connectors/server.py`. It reads only `Tool.fn`, `Tool.name` and
  `Tool.is_async` — three public fields on a public class — and is inert when publishing is off.
- **Every test added here was confirmed to fail against the code it replaces before being kept**:
  removing the installer takes the two tool-hook tests red; restoring `"hartree/bohr"` takes the
  unit test red; removing the `xtb.hess` prefix takes three red; emptying `TOOL_COMPOSITES` takes
  two red. Each starts at a hook — a real cache miss through `cached_compute`, a real tool call
  through the MCP tool manager with the hook installed the way `connector_app` installs it — because
  `D-2026-08-26-a-route-is-not-a-shape` records what starting at a projector proves, which is
  nothing.

## Left open

- **`tests/calc_server_fake.py::_KEYED` does not declare `compute_atomic_descriptors` or
  `compute_surface_potential`**, both of which go through `cached_remote` in production. Nothing is
  broken by it — both `calc_type` prefixes route — but `test_every_calc_type_the_server_stamps_…`
  is not evidence about them, and the fake is this repository's statement of the server's key
  contract.
- **`test_publish_projection.py`'s docstring measurement is now stale** in one direction: it records
  that all 79 `_fact` call sites pass an already-canonical unit and that a runtime probe saw exactly
  one conversion. Two call sites now convert on a live path, which is the state that docstring
  argued for.
- Unchanged from D-2026-08-25 and D-2026-08-26: no deployment points at a real results database, and
  nothing has measured rows-per-calculation on a real corpus.

## Alternatives rejected

**A projector for `xtb.hess` that derives the frequencies.** It would have to diagonalize a
mass-weighted matrix, which needs the elements the row does not carry, and would put physics in the
one module whose job is vocabulary translation. Measured: the row has no path to a mass.

**Publish `ThermochemistryResult` off the Hessian's own hook.** The Hessian is published *before* the
RRHO arithmetic runs, and the arithmetic's inputs — temperature, symmetry number, standard state —
are the caller's, not the row's. Publishing one would mean inventing them, which is the "a number is
never guessed" rule `project.py` is built on.

**A `publish_composite()` call at the end of each composite in `connectors/calc/compose.py`.** The
`audit_events.agent` shape: correct on the day it is written, and silently incomplete the first time
somebody adds a composite. It also could not reach `predict_logd`, which composes in the tool body.

**Publish every model crossing the tool boundary.** Simpler, and wrong: most of them already
published from the cache hook under a real cache key, so it would write a second record of one
calculation under a second identity — the duplication the whole seam is keyed to avoid.

**Hook `ToolManager.call_tool`, where two patches already live.** The result there has been through
`convert_result`; recovering the model would mean parsing content blocks or resolving each tool's
return annotation, i.e. depending on a *shape* upstream never promised in place of three fields it
publishes.
