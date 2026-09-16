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

The sibling's policy is a **flat list**: `no_egress.FORBIDDEN_MODULES`, fifteen network roots, banned
everywhere, with two argued exemptions in `src/`. `TID253` expresses that exactly — one config block,
one banned list, two `noqa`s carrying their reason at the import.

This repository's policy is a **matrix**. Measured on `tests/test_third_party_layering.py`:

| | |
| --- | --- |
| third-party roots mapped to a stack | 32 |
| distinct stacks | 15 |
| declared `(package, stack)` edges | **51** |
| first-party packages appearing in those rows | 15 |

`postgres` is reachable by 11 packages, `httpx` by 8, `langgraph` by 8, `rdkit` by 4 — and `units`,
`token`, `ml`, `tokenizer` and `warehouse` by exactly one each. The rule is not "nobody imports X", it
is "these eleven may import X and the other four may not".

## Why that cannot be a second belt here

`TID253` bans globally and is narrowed by `per-file-ignores`. Expressing the matrix therefore means
**51 glob rows restating the 51 declared edges**, in a different syntax, in a different file. That is
precisely the shape `D-2026-09-13-the-rule-that-would-have-caught-it-was-not-the-one-asked-for`
refused for `S101`: *a second declaration of one rule with nothing reconciling the two*. It could be
reconciled — a test comparing the ruff config against `_ALLOWED_MODULE_STACKS` in both directions —
but then the belt costs a duplicated table **plus** the machinery that proves the duplicate is honest,
to buy editor feedback.

And it would only ever be a *partial* belt, which is the argument that actually settles it. This
policy distinguishes **three** import scopes deliberately — module, function (`_ALLOWED_LAZY_STACKS`)
and `TYPE_CHECKING` — and `TID253` sees one. The test has to stay whole for the other two, so nothing
is retired, nothing is simplified, and the tree gains a second partial statement of a rule it already
states completely in one place.

## What is taken instead

The half that genuinely was missing is **coverage of the roots themselves**, and it was missing three
times in one week. An *unmapped* root is skipped by the walk entirely, so a package could import it
freely and the policy would say nothing: `httpx_sse`, `pint` (with `flexparser` and `flexcache`) and
`tiktoken` all entered this tree during this review and all three were invisible until they were
mapped. Two of the three were mapped by the agent that introduced them only because the gap had just
been found by the first.

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
