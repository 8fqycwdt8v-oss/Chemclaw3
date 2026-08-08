# D-2026-08-08-a-rule-with-no-test-is-a-claim — the enforcement layer for rules this repository already states

**Status:** accepted

## Context

This is lane T9 of the 2026-08-08 review campaign, and it is a different shape from the other
lanes. Almost nothing here is a behaviour bug found in production code. It is the set of rules this
repository *states* — in `CLAUDE.md`, in package READMEs, in a nine-line comment that records a
measured soak failure — and does not check. Five adversarial review rounds had by then found that
roughly a third of the campaign's new defects were introduced by the campaign's own fixes, which
makes the difference between a stated rule and a checked one the highest-leverage thing left.

Seven findings, each verified before being fixed.

**1. The layering test was blind to every third-party import.** `tests/test_layering.py` AST-walks
the real import graph, which is the right design, and then records an edge only
`if target.startswith("chemclaw")`. So it enforced the first-party half of the layering rules and
none of the third-party half — which is the half the architecture documents actually state:
"Durability lives **only** in Temporal, never in MAF"; "merging them would put Temporal imports
inside the physics". `science/README.md:8` claims "None of these import Temporal, MCP, FastAPI or
`chemclaw.agent` … and `tests/test_layering.py` keeps it that way". Measured: `science/` is clean,
so the claim is true — and only its last clause, about the test, was false. `import temporalio` in
`science/`, `import agent_framework` in `durable/` and `import fastapi` in `kg/` all passed every
test in the repository.

**2. The one `except: pass` had an unenforced precondition.** `core/metrics_bridge.py:35` is the
repository's only bare swallow, and its argument is correct: `Metrics.increment` raises `KeyError`
on an undeclared name, and that strictness must not reach a request path. The consequence is that a
mistyped counter name is invisible at *every* level including DEBUG — swallowed, unlogged, never
emitted. No call site named an undeclared metric when measured. Nothing made that true.

**3. `deps-audit` was in neither `make ci` nor `.github/workflows/ci.yml`.** It ran only from
`image.yml`, which triggers on `push: main` and `pull_request`. Every branch push, and the whole
documented pre-push gate, went green against the lockfile — which at the time of this review carried
two known CVEs in `pypdf`. CLAUDE.md's "a green `make` locally means a green CI" was false for the
supply chain specifically.

**4. The jitter fix had landed in one of three copies.** The three live-harness modules vary a
temperature per process so a rerun cannot be answered from the calculation cache (a durable job's id
is a hash of its payload; a duplicate launch rejoins rather than recomputes, D-011).
`cli/storm_behaviours.py` carries the reasoned modulus and a nine-line comment recording how it was
learned — `% 719` recurs every ~12 minutes and made 6 of 81 soak rounds report "0 job_records row(s)
written", which was the cache working correctly and being read as a failure. `cli/live_storm.py`
still had `% 971` (16.2 minutes, 1.35x the period already measured failing). `cli/live_jobs.py` had
`% 25`: **25 distinct temperatures that ever exist**, so after ~25 runs against one database that
lane is green forever while computing nothing — the exact failure its own comment says it exists to
remove.

**5. Warn-and-degrade sites that count nothing.** The review reported 22 across 17 modules; a
broader re-measurement here — every `except` handler that logs a warning and does not re-raise —
finds **42 across 35 modules, of which exactly 3 count anything** (`durable/publish.py`,
`kg/graph.py`, `kg/proposal.py`). Each swallow is individually right; the alternative is failing a
chemist's turn because a preference did not persist. From outside, a preference store that has
stopped writing, a cost ledger losing every row and a redaction filter that never resolved its
connector token names are indistinguishable from a healthy service. `agent/audit.py:307` had already
established the house pattern — count it, then log it under a stable marker — for exactly one site.

**6. `durable/heartbeat.beating` had three holdouts that disagreed with it.**
`durable/document_sync.py` (twice) and `durable/eln_sync.py` hand-rolled the beat loop with
`timeout / 3` and **no floor**, where the helper uses `max(1.0, timeout / 4)`.
`document_sync_heartbeat_timeout_seconds` is declared as a bare `float` with no `Field(gt=0)`, so a
sub-second ENV value beat several times a second against the Temporal server for a whole chunk, and
a negative one made `asyncio.sleep` return immediately — an unbounded busy loop. The floor is
precisely what those copies were written before and never picked up.

**7. `template-validate` checked step names and never step arguments.** Proved by mutation: renaming
`smiles` to `smilez` in the shipped `hazard-briefing` template and adding `nonexistent_arg: 42`
beside it printed "template validation passed". A template is a *pinned* procedure, which is the
stated reason the name check exists; the same reason applies to the arguments, and the gap between
them was the whole distance from "validated" to "can run".

## Decision

Land the enforcement, and where a rule is currently violated, **say so in the enforcement rather
than widening it to fit**.

**`tests/test_third_party_layering.py`** is the missing half of the layering policy: the same AST
walk, bucketed by scope, checked against a stack-level policy. It carries **three dictionaries and
not one**, because an allow-list that mixes "this stack is that layer's job" with "this is a
violation nobody has fixed" stops meaning anything:

- `_ALLOWED_MODULE_STACKS` — the package owns the stack, with the sentence that says so.
- `_ALLOWED_LAZY_STACKS` — function scope only, deliberately.
- `_KNOWN_LEAKS` — the architecture forbids it, it exists, and the row names why it is still here.
  Keyed by **file**, not by package, so a third module joining an existing leak fails.

Every row is pinned in both directions: a fixed leak fails the build until its row goes with it.
Three leaks are recorded rather than blessed — `agent/durable_tools.py`, `agent/interaction_tools.py`
and `templates/registry.py` importing `temporalio` — because the root cause is the five-copy launch
idiom and D-2026-08-08-an-outage-is-not-a-missing-job established that no single reuse policy can
serve all five callers. `durable → maf` **is** declared as legitimate: `template_activities` must
build MAF's invocation context to run a tool as a template step, and `retention` deserialises stored
session rows with `Message.from_dict` so it can reuse `agent.message_pairing.droppable_rows`; both
follow from the `durable → agent` edge `test_layering.py` already declares, and an agent message
*is* a MAF object.

Writing the private-import rule turned three of the five findings it was written for into deletions.
The review recorded five imports of `agent_framework._harness.*` / `._compaction`; asking the
installed package rather than reading the comments beside them showed that `todos_remaining`,
`AgentModeProvider`/`get_agent_mode`/`set_agent_mode` and all five compaction names **are** exported
at the package top level in 1.11.0, and are the identical objects
(`af.todos_remaining is _harness._loop.todos_remaining`). `chemclaw_agent.py` carried a comment
asserting the opposite — one more instance of the standing lesson that prose in this repository is
evidence about what its author believed. Those three now import from `agent_framework` and their
rows are gone; `_KNOWN_PRIVATE_IMPORTS` holds the two symbols that genuinely are not exported
(`ShouldContinueCallable`/`ShouldContinueResult`, in `loop_cap.py` and `plan_gate.py`), keyed by
`(file, target)` so a third fails.

`tests/test_layering.py` also stops treating `if TYPE_CHECKING:` as an exemption. The skip guarded
**zero** cross-package imports, so it was dead code documenting a working way around every other
check in the file; those imports are now a third scope, declared like any other.

**`tests/test_metric_declarations.py`** checks metric names in both directions. Forward: a literal
at an `increment`/`observe`/`bind_gauge` call site is in the matching registry. Backward: every
declared metric appears as a literal somewhere in `src/` — which is what covers the one call site
whose name is a variable (`api/runner.py` loops over the four priced token counters), because a typo
there shows up as the real name losing its last mention. Literal `labels={…}` key sets are checked
against `_COUNTER_LABELS` for the same reason: that `KeyError` is swallowed too.

**`deps-audit` joins `make ci` and the `check` job in `ci.yml`**, last in both so a dependency
finding cannot mask a broken test, and `tests/test_deploy_chart.py` pins both wirings.

**The jitter modulus is applied to the two stale copies and no helper is built.** The three lanes
legitimately want different base temperatures (298.15 K, 300.0 K), and a shared helper would hide
which modulus each got — which is how one of them came to have `% 25`.
`tests/test_run_jitter.py` finds the expressions rather than being told where they are and
*evaluates* each across a 24-hour window, so it pins the property ("no payload repeats within the
longest soak") rather than the literal.

**`core/metrics_bridge.degraded(logger, subsystem, message, …)`** is the house pattern with one
owner. It lives beside `record_metric` rather than in `core/metrics.py` because it is called from
inside `except` blocks — the one place a raising metric update would replace the degradation being
reported with a `KeyError` from the reporting — and `core/metrics.py` cannot import the bridge
without a cycle. It takes the **caller's** logger, so the record still names the module that
degraded. One counter with a `subsystem` label, not one counter per site: the operator question is
`sum by (subsystem)`, and a per-site counter makes that a union of metric names that has to be
edited every time a site is added. `agent/audit.py`'s dedicated
`chemclaw_audit_sink_failures_total` stays as it is — a lost GxP record is a named regulatory fact
with its own alert, not a member of a general family.

**The three heartbeat holdouts adopt `beating()`**, keeping their eager pre-beat (a fast chunk can
finish before `beating()`'s first interval elapses, which the helper does not provide). A new test
asserts the beat interval has exactly **one** derivation in the tree: no module may divide a
heartbeat timeout by hand again, because the helper's existence is demonstrably not what prevents
the next copy.

Adopting it turned up a defect *in the helper*, which had to be fixed for the adoption not to be a
regression. `beating()` runs the awaitable as a task so the timer can run beside it, and
`asyncio.wait` does not cancel what it was waiting on when the waiter is cancelled — so a cancelled
activity returned while its real work carried on detached, still committing. The two sync
activities previously awaited their work directly, where cancellation propagates. `beating()` now
cancels the inner task and re-raises, which is what makes `beating(x)` behave-alike to `await x`
and is the only thing a caller wrapping an existing `await` in it can reasonably assume. This is a
fix for the three connector call sites too.

Two versions of the test for that fix passed against the unfixed helper before the third one did,
and both failures are recorded in its docstring rather than deleted: the first asserted the work had
not *finished* 50 ms after cancellation, which a 5-second sleep satisfies either way; the second
asserted after `asyncio.run`, which cancels every pending task on its way out. The property has to
be asked of the wrapped coroutine, inside the loop.

**`template-validate` checks argument keys** for every tool whose implementation is a function in
this tree — the in-process `@tool` registry and each bundle's own server tools module, which covers
every tool the shipped templates call. Keys only, never values: a template argument may be a
`${…}` reference whose type is known only at substitution. An unresolvable tool (a template
launcher's generated params model, a skill tool) is skipped rather than guessed at.

## Consequences

- Two layering policies now exist and are checked independently. The third-party one records the
  `agent → temporal` leaks and two private imports as debt; it fails the build when another file
  joins a leak, and when a leak is fixed and its row is not deleted.

  **It caught one within the hour.** Written against three leaks, it failed on merge with a
  fourth — `agent/job_results.py`, added by a parallel lane of this same campaign after this test
  was drafted. That is the by-file keying earning its cost: a policy keyed by package would have
  blessed `chemclaw.agent → temporal` wholesale and absorbed the new one in silence, which is the
  exact failure this file exists to prevent. It is recorded as debt, not fixed, for the same
  reason as its three siblings.
- **`deps-audit` is wired into `make ci` and `ci.yml`, and is green.** It was red when this lane
  wrote it — two real CVEs against the lockfile — and the `pypdf 6.15.0` bump that closes them
  belonged to another lane of the same campaign. Both are now merged, and `make deps-audit`
  reports "No known vulnerabilities found". The finding stands: the pre-push gate every branch
  push ran had no supply-chain check at all, which is how 6.14.2 sat in the lock.
- Ten log sites moved from `logger.warning` to `degraded(...)`, eight of them therefore from WARNING
  to ERROR. That is a deliberate operational change: a degradation is not a caution about something
  that might matter later. Two sites pass `level=logging.WARNING` and each says why where it passes
  it (a cosmetic mode badge; a skill manifest already gated by `make skill-validate`).
- `chemclaw_degraded_total` is a new exported series family. Its label space is nine values, all
  string literals at call sites, and `tests/test_degraded.py` enumerates them from the source.
- Ten of the fifteen uncounted sites in this lane's packages are converted. The five left, and the
  twenty-four in other lanes' packages, are recorded in `BACKLOG.md` in three groups with three
  different reasons: four are **workflow code** and need the `workflow.unsafe.is_replaying()` guard
  `durable/publish.py` already demonstrates (a `core` helper may not import `temporalio` to know
  about replay); one is a **CLI**, whose process exits before any scrape reaches its registry; and
  the rest are in `api/`, `ingest/`, `science/`, `retrieval/`, `evals/` and `memory/`, which other
  lanes of this campaign were editing in parallel.
- `template-validate` now imports each bundle's server tools module, which pulls `mcp` and the
  science engines into that CI step. Seconds, on a gate that already imports the agent package.
