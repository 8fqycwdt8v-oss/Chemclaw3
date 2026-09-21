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
- `_refuse_an_unbounded_expansion` runs **inside** `_compiled`, because `regex` expands a bounded
  repeat and `re` does not — see the costs below.
- Exceeding the budget raises a new `PatternBudgetError`, which descends from `Exception` rather
  than from `ChemclawError` so that no reject-and-continue handler on the ingest path swallows it,
  and is listed in `durable/publish._BAD_DATA_TYPES` by name.
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

**`regex` is not a superset of `re`, and an earlier draft of this ADR said it was.** That sentence
was wrong and the hedge on it ("not bit-identical about every malformed input") did not cover the
real cases, which are not malformed. A differential fuzz over 400,000 patterns found **578** that
`re.compile` accepts and `regex.compile` rejects, and they are all one shape — `{name}` is
`regex`'s **fuzzy-matching** syntax:

```
r'{sample} (\S+)'   re=ok   regex=error: expected } at position 2
r'{id}=(\d+)'       re=ok   regex=error: expected } at position 2
r'\d+ {solvent}'    re=ok   regex=error: expected } at position 6
```

A `datasource.yaml` pulling a value out of placeholder-shaped free text is exactly that shape. The
failure direction is the safe one — at load, naming itself, before a row is read — but a site with
such a pattern has to rewrite it, and that is a migration cost rather than a footnote.

Two changes are **silent**, which is worse, and are recorded here because no gate can catch them:

| pattern | subject | `re` | `regex` |
| --- | --- | --- | --- |
| `[[:alpha:]]` | `"x"` | no match (a literal set) | **match** (a POSIX class) |
| `[\s]` | `"\x1c"` | **match** (ASCII separators) | no match (Unicode whitespace) |

Both are patterns both engines accept and read differently. `re` itself emits a `FutureWarning` on
the first, so it is dubious under either engine; the second is a genuine narrowing of `\s`. A site
whose binding depends on either gets a different extraction with no error anywhere.

**A compile-time cost that is not bounded by the match budget.** `regex` **expands** a bounded
repeat where `re` does not:

| pattern | `re.compile` | `regex.compile` | peak RSS |
| --- | --- | --- | --- |
| `a{100}` | 0.058 ms | 0.172 ms | — |
| `a{10000}` | 0.082 ms | 2.9 ms | — |
| `a{100000}` | 0.102 ms | 34 ms | — |
| `a{1000000}` | 0.110 ms | **431 ms** | **290 MB** |
| `a{100000000}` | 0.110 ms | did not finish in 2 min | — |

That runs in `_check_pattern`, at binding load, on manifest text nobody here wrote — outside
`eln_regex_timeout_seconds`, which bounds a *match*, and outside every Temporal deadline. So the
first version of this change let a `datasource.yaml` take an ingest worker down before a single row
was read, on a pattern that was free under `re`. `_refuse_an_unbounded_expansion` is the answer: a
text scan for literal repeat counts over `_MAX_REPEAT_COUNT` (10,000, about 3 ms to compile and
four orders above what a binding writes), run **inside** `_compiled` so every route to a compiled
pattern passes it. Scanned rather than compiled, because compiling is the thing being guarded; and
the scan tracks backslash escapes and character classes, because `[{]{1}` and `\{100000\}` are
literals every engine reads as such and a bare `finditer` would refuse them.

`regex` also recurses where `re` iterates, so ~180 nested groups raise `RecursionError` on a
pattern `re` compiles. Caught and reported as a `PathSyntaxError` rather than escaping as an
interpreter error.

**A dependency line.** `regex` was already in this repository's closure as a requirement of
`tiktoken`, a runtime dependency, and is in `uv export --frozen --no-dev` — so the cost is the
declaration, which is exactly
`D-2026-09-16-a-library-already-in-the-closure-is-a-declaration-not-a-dependency`'s case, including
its correction about what "in the closure" has to mean. Declared rather than left transitive for
the reason `tiktoken`'s own comment gives one line above: a bump over there that drops it would
take a **bound** with it, silently.

### Why the refusal is not a bad row, and how the first version of this got it wrong

The cost belongs to the *pattern*, so a reject-and-continue handler is the wrong reader for it:
skipping the row re-runs the same unfinishable match against every remaining row of every remaining
page — one stall becoming `rows × budget` of them, each booked as a data refusal, with the ingest
still never finishing.

**The first version of this change claimed to have prevented that and had not.** It made
`PatternBudgetError` a `ChemclawError` and argued it escaped the per-entry handler because it was
not an `ElnMappingError` — a claim checked against the `ElnMappingError` arm in
`warehouse/adapter.py`, which is the wrong handler. A transform runs under
`ingest/eln/sync.py`'s `except (ChemclawError, ValidationError)`, one layer further out. Driven on
the real `sync_entries` with a `(a+)+$` transform over ten entries at a 0.05 s budget: nothing
escaped, all ten were booked as data refusals, and the page cost **0.503 s** — `rows × budget`
exactly, the outcome the class exists to prevent, shipped under a green test that asserted the
wrong non-membership.

So `PatternBudgetError` descends from `Exception`, not from `ChemclawError`, on
`SubsystemUnavailableError`'s precedent and the same argument: this is not bad *data*. And the
assertion is no longer a named non-membership. `test_no_handler_on_the_ingest_path_catches_a_
pattern_that_cannot_finish` resolves every `except` clause under `ingest/eln/` to the classes it
actually catches and requires that none of them is a base of this one — which found a **second**
handler the prose had never mentioned, the replay path at `sync.py:362`.

Listed in `_BAD_DATA_TYPES` by **name**, because Temporal matches the outermost failure's class
name; leaving the hierarchy does not remove it from that list, and ancestry never put it there.

### What this does not bound

A pattern that is slow on every cell but always finishes *inside* the budget is not refused, and
the per-cell budget does not add up to a page bound: `_read` runs once per reaction field, per
attribute, and per component and impurity **row**, so a page is `eln_sync_batch_size ×
cells_per_entry` matches. At the shipped 100-entry batch, a pattern spending most of its 0.25 s on
each of twenty cells per entry is 500 s — past `eln_sync_timeout_seconds` and past the heartbeat,
because `map_to_ord` is synchronous CPU work that no asyncio timer can interrupt.

That case is **pre-existing** rather than introduced here — the same pattern under `re` cost the
same page, ~3x faster — and this change makes it ~3x worse per cell while removing the catastrophic
case entirely. Bounding it properly means a cumulative per-entry or per-activity budget, which
trades refusing an honest slow pattern against bounding total work and is a decision of its own
rather than a line in this one. It is a `BACKLOG.md` row, not a claim made here.

## Consequences

- One catastrophic pattern costs one activity failure with a message a site can act on, instead of
  an activity timeout followed by an identical retry.
- Every regex transform costs about 0.8 µs more per cell.
- `eln_regex_timeout_seconds` is the knob for a site whose pattern is genuinely expensive, and the
  refusal names it.
- A literal repeat count over 10,000 is refused at load, where under `re` it was free.
- A site whose pattern uses `{name}`, `[[:alpha:]]` or relies on `\s` matching `\x1c`–`\x1f` gets a
  different answer — the first as a refusal at load, the other two silently.
- A slow-but-finishing pattern still costs the page, unchanged in kind and ~3x in degree.
- The static amplifier check stays unbuilt for the *ambiguity* class and is now optional rather
  than load-bearing; the one unambiguous amplifier (a literal repeat count) is checked, because the
  second engine made it a hazard rather than because the first argument changed.

**Revisit when:** a site reports a legitimate pattern refused by the budget or by the repeat cap —
at which point the question is whether the default is too tight or whether the per-cell budget
should be a per-entry one — or `re` itself gains a deadline, which would make the second engine
unnecessary and take all three of the costs above with it.
