# D-2026-09-21-a-pattern-that-cannot-be-timed-out-is-run-by-an-engine-that-can — the site-supplied regex in a warehouse binding

**Status:** accepted · **Date:** 2026-09-21 · Supersedes nothing. Closes the `BACKLOG.md` row *"A
site-supplied regex from a datasource manifest runs against warehouse cell text with no timeout,
so a catastrophic pattern hangs the ingest activity"*, which named three candidate remedies and
asked for one to be chosen.

## Context

`ingest/eln/warehouse/expr.py::_regex` takes `options["pattern"]` straight out of a site's
`datasource.yaml` and runs `re.search` over `as_text(value)` — one free-text warehouse cell — once
per bound field per row. Neither factor is this repository's:

- the **pattern** is a site's, checked for *syntax* by `_check_pattern` and never for how much work
  it can be made to do;
- the **subject** is a warehouse column, whose length is whatever the site's schema allows.

Python's `re` has no timeout at any layer, so the only bound on the product was the ingest
activity's `start_to_close` — after which the retry ran the identical pattern over the identical
page. That is the `_BAD_DATA_TYPES` argument in reverse: a deterministic failure retried as though
waiting could change it.

Driven, so the cost is a measurement rather than a category:
`re.search("(a+)+$", "a" * 3000 + "b")` was **still running when a 120 s alarm killed it**. Its
duration is not a number anybody has.

This is operator-controlled input, not the untrusted-input case the rest of that backlog section
holds, which is why it was queued separately from the `core/logging.py` ReDoS rather than fixed in
the same commit. The remedies are genuinely different: `core/logging.py`'s patterns are
*first-party*, so bounding every quantifier in them is a fix somebody here can make and keep
(`_OPAQUE{0,255}`, the bounded lookahead, the growth test). Nobody here writes the pattern in a
`datasource.yaml`.

## Decision

**Run this one transform under `regex`, with `timeout=`.** `re` stays the engine everywhere else
in the module and everywhere else in the tree.

- `core/config/eln.py` gains `eln_regex_timeout_seconds`, default **0.25**.
- `_compiled` is an `lru_cache`d `regex.compile`, and `_check_pattern` goes through it too, so the
  engine that accepts a pattern at load is the engine that runs it.
- Exceeding the budget raises a new `PatternBudgetError`, which is **not** an `ElnMappingError`
  and is listed in `durable/publish._BAD_DATA_TYPES`.
- `regex` is declared in `[project.dependencies]` and `types-regex` in the dev group.

### Why not the two alternatives the row named

**Bounding the input** — `as_text(value)[:n]` — is the cheapest thing this seam can do without a
dependency, and the row already called it a mitigation. It is worse than that: the damage is
*exponential* in the input for the classic shapes, so the bound would have to be around 30
characters to matter, which is not a bound on a free-text column, it is a deletion of one. A cap
that leaves `(a+)+$` unfinishable at 60 characters has bought nothing and reads like a fix.

**Rejecting a pattern whose shape is a known amplifier at `datasource-validate` time** is the one
that fits this module's own promise — "a typo fails at startup, not on row 40,000". It was
prototyped far enough to price: `re._parser.parse()` gives a real AST and a nested-unbounded-repeat
walk over it is short. It was declined on both error directions at once. **False negatives**,
because ambiguity rather than nesting is the actual condition — `(a{1,5})+` is bounded at every
quantifier and still catastrophic — so the check would be advertised as a gate while being a
heuristic. **False positives** are the worse half and are the shape `tasks/lessons.md` rule 96 is
about: a warning that fires on correct input reads as a working one, and the rule would refuse
legitimate site patterns at load with no way for the site to say "this one is fine". A static check
that cannot be right in either direction is not the gate this seam needs; it could be added later
*on top of* a real bound, where being approximate is free.

### Why the third remedy is a real bound and not a third mitigation

`regex` checks its deadline inside its own matching loop, in-thread. That matters because the
obvious spelling of "with a wall clock" — `asyncio.to_thread` plus a timeout — does not work here
at all: `sre` does not release the GIL, so the caller would be released while the thread stayed
pinned forever, and the ingest would leak a worker thread per catastrophic cell rather than stall
on one.

Measured, the four catastrophic patterns tried (`(a+)+$`, `(a|a)*$`, `^(a|aa)+$`, `(x+x+)+y`, each
against 3,000 characters) return at **0.250–0.252 s** against a 0.25 s budget.

### The costs, stated

**Per-call cost.** `regex` is slower than `re` on the ordinary case. Measured on this box, per
call, over the three pattern shapes a binding actually writes:

| | `re` | `regex` (string) | `regex` (cached compile) |
| --- | --- | --- | --- |
| `L-(\d+)` | 0.38 µs | 4.62 µs | **1.19 µs** |
| `([A-Z]{2,4}-\d{3,6})` | 0.47 µs | 3.91 µs | — |
| `(\d+(?:\.\d+)?)\s*%` | 0.65 µs | 4.19 µs | — |

Most of the gap is `regex`'s per-call cache lookup, which is why `_compiled` exists: through it the
swap costs about **0.8 µs per cell**, roughly 3x `re` and about 0.1 s on a 10,000-row page with
three regex-bound fields — against a warehouse fetch, nothing. On the case that actually scans, a
full 1 MB cell with no match, `regex` is **faster** (0.24 ms against 0.34 ms).

**A behaviour change at load.** `regex` in its default version is a superset of `re`, so a pattern
that compiles today compiles tomorrow — but the two libraries are not bit-identical about every
malformed input, and `_check_pattern` now reports `regex`'s message. A site whose pattern `re`
accepted and `regex` does not would newly fail at load, naming itself. That is the right direction
(the alternative is failing on row 1) and it is a change, so it is written here.

**A dependency line.** `regex` was already in this repository's closure as a requirement of
`tiktoken`, a runtime dependency, and is in `uv export --frozen --no-dev` — so the cost is the
declaration, which is exactly
`D-2026-09-16-a-library-already-in-the-closure-is-a-declaration-not-a-dependency`'s case, including
its correction about what "in the closure" has to mean. Declared rather than left transitive for
the reason `tiktoken`'s own comment gives one line above: a bump over there that drops it would
take a **bound** with it, silently.

### Why the refusal is not a bad row

`warehouse/adapter.py` catches `ElnMappingError` per entry and skips the row. That is right for a
NULL timestamp and exactly wrong for this: the cost belongs to the *pattern*, so skipping and
continuing would re-run the same unfinishable match against every remaining row of every remaining
page — one stall becoming `rows × budget` of them, each booked as a data refusal, with the ingest
still never finishing. So `PatternBudgetError` descends from `ChemclawError` directly, escapes the
per-entry handler, and fails the ingest naming the pattern, the cell length and the ceiling.

Listed in `_BAD_DATA_TYPES` by **name**, because Temporal matches the outermost failure's class
name and inheriting from a listed class buys nothing.

## Consequences

- One catastrophic pattern costs one activity failure with a message a site can act on, instead of
  an activity timeout followed by an identical retry.
- Every regex transform costs about 0.8 µs more per cell.
- `eln_regex_timeout_seconds` is the knob for a site whose pattern is genuinely expensive, and the
  refusal names it.
- The static amplifier check stays unbuilt and is now optional rather than load-bearing.

**Revisit when:** a site reports a legitimate pattern refused by the budget — at which point the
question is whether the default is too tight or whether the per-cell budget should be a per-page
one — or `re` itself gains a deadline, which would make the second engine unnecessary.
