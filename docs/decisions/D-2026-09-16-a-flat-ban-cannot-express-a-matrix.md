# D-2026-09-16-a-flat-ban-cannot-express-a-matrix — ruff's banned-module-level-imports is declined here, and adopted next door

A dependency audit proposed ruff's `flake8-tidy-imports` `banned-module-level-imports` (`TID253`) as a
**second belt** beside `tests/test_third_party_layering.py`. The pitch is good: the rule bans a module
at *module scope only* while permitting a function-scope import, which is exactly the distinction that
file hand-builds, and ruff is already installed and already run by `make lint`, so the feedback would
arrive in an editor rather than at the end of a suite.

`Chemclaw3-mcp` adopted it in the same review wave. This repository declines it. The difference is not
taste, and it is worth writing down because the two trees look similar enough that somebody will
propose it again.

## What was measured

The sibling's policy is a **flat list**: one set of network roots, banned everywhere, with two argued
exemptions in `src/`. `TID253` expresses that exactly — one config block, one banned list, two
`noqa`s carrying their reason at the import. How long that list is is deliberately not written here:
it lives in `Chemclaw3-mcp`'s `mcp_server_kit.no_egress.FORBIDDEN_MODULES`, mirrored into that
repository's own `banned-module-level-imports`, and this repository neither builds it nor watches it
— the same reason `SERVED_ELSEWHERE_ALLOWANCE` exists rather than a transcribed token count. A first
draft of this line said fifteen and the list held sixteen, which is that rule earning itself again.

This repository's policy is a **matrix**, and the shape rather than any one figure is what decides
this. Read off `tests/test_third_party_layering.py`, which is where the live numbers are:

- `_STACKS` maps each third-party root to a stack — several dozen roots onto seventeen stacks.
- `_ALLOWED_MODULE_STACKS` holds one row per `(first-party package, stack)` permitted at **module
  scope**; `_ALLOWED_LAZY_STACKS` holds the rows permitted only at function scope. The two sets
  overlap, so their union is smaller than their sum, and a cost argued against the union is argued
  against a basis `TID253` cannot see.
- Two first-party packages under `src/chemclaw/` appear in no row at all, which is the strongest
  form the matrix takes: for them every mapped root is banned.

The distribution is the point. `postgres` is reachable by eleven packages and `langgraph` and `httpx`
by eight, while `units`, `token`, `ml`, `tokenizer`, `warehouse` and `share` are reachable by exactly
one each. The rule is never "nobody imports X"; it is "these eleven may import X and the rest may
not", with a different eleven per stack.

## Why that cannot be a second belt here

**The obvious objection is that the matrix is expensive to restate, and the real one is that it
cannot be restated at all.** The first draft of this section said the belt would cost "51 glob rows
restating the 51 declared edges", and both halves were wrong. The basis was wrong — `TID253` bans at
module scope only, so what it could ever restate is `_ALLOWED_MODULE_STACKS` alone, not its union
with the lazy rows. And the mechanism does not exist: ruff's `per-file-ignores` maps a glob to **rule
codes**, and `TID253` is one code covering the entire banned list. Ignoring it for a path switches
the whole ban off there — a package permitted `postgres` would be permitted every other banned root
in the same breath. A per-`(package, stack)` exemption is not expressible in 51 rows, or in any
number of them.

The mechanism that *does* vary a banned list by path is ruff's hierarchical configuration — a nested
`ruff.toml` per directory, `extend`-ing the root and restating the setting. Driven on this ruff to
check rather than assume: it works. What it costs is the matrix **transposed into its complement**,
because a nested config declares what that package may not import: one config file per first-party
package under `src/chemclaw/`, each listing every mapped root minus that package's own stacks —
measured against the tree this decision was taken on, an order of magnitude more entries than the
dict they duplicate, spread over eighteen files instead of one.

Either way it is the shape `D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for`
refused for `S101`: *a second declaration of one rule with nothing reconciling the two*. It could be
reconciled — a test generating the ruff configs from `_ALLOWED_MODULE_STACKS` and failing when they
drift — but then the belt costs a duplicated table **plus** the machinery that proves the duplicate
is honest, to buy editor feedback.

And it would only ever be a *partial* belt, which is the argument that actually settles it. This
policy distinguishes **three** import scopes deliberately — module, function (`_ALLOWED_LAZY_STACKS`)
and `TYPE_CHECKING` — and `TID253` sees one. The test has to stay whole for the other two, so nothing
is retired, nothing is simplified, and the tree gains a second partial statement of a rule it already
states completely in one place.

## What is taken instead

The half that genuinely was missing is **coverage of the roots themselves**. An *unmapped* root is
skipped by the walk entirely, so a package could import it freely and the policy would say nothing.
Five libraries entered this tree during this review — `httpx_sse`, `pint` (with `flexparser` and
`flexcache`), `tiktoken`, `pathspec` and `charset_normalizer` — and every one of them was invisible
to the policy until it was mapped.

**Three were mapped and two were not, and the first version of this paragraph named only the three.**
`pathspec` and `charset_normalizer` are module-scope imports in `ingest/documents/`, added by this
same wave, absent from the map, and absent from the sentence whose subject is *a new root was never
banned from anything* — the argument happening to its own record while it was being written down.
Mapping them then turned up `scipy`, which no wave introduced: a root imported by three packages
since long before this review and never mapped, so the blind spot stated for new roots was never
only about new ones.

That is the real failure mode — not "a banned import slipped through module scope", which the test
already catches, but "a new root was never banned from anything". It is answered where the policy
lives, by the mapping being a required step, and `test_no_declared_module_stack_is_stale` is what
keeps a row from outliving its import.

## What would change this

`TID253` becomes the right tool the day the matrix collapses toward a list — if the stacks with a
single permitted package grew to dominate, a global ban with a handful of exemptions would express the
policy rather than shadow it. It is also the right tool for any *new* rule that genuinely is flat.
Neither is true today.

## What keeps it true

- `tests/test_third_party_layering.py` — the whole policy, all three scopes, pinned in both directions
- `tests/test_third_party_layering.py::test_no_declared_module_stack_is_stale`
- `tests/test_layering.py` — the first-party direction, which this rule never touched
