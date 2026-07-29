# D-139 — Three silent failures: a degraded turn, a pooled calibration, and two counters wired to nothing

**Status:** accepted · **Context:** the fourth batch of the agentic-system review
(`docs/audit/2026-07-agentic-system-review.md`), taking the items whose common shape is that the
system was already *wrong* and said nothing. None of the three produced an error, a red test or a
failed turn; each produced a plausible answer or a plausible number.

**Decision.** Announce the degradation at the one place that can see it, scope the calibration read
to the version that produced the numbers, and increment the counters that were declared and never
written.

### 1. A turn that lost its connectors answered as if it had them (REV-6)

`connectors.registry.open_reachable` returns "the names of the connectors that are not connected,
**for the caller to surface**". All four callers — `service/runner.py`, `agents/cli.py`, and both
activities in `workflows/template_activities.py` — called it bare and discarded the list.

This is the quietest failure found in the review, because nothing in the system is in a position to
notice it. A connector that is down contributes no tools; the model is handed a shorter list and has
no way to know it is shorter, so it reasons from what remains and answers confidently. "The ELN has
nothing on that batch" and "the ELN was unreachable" arrive as the same sentence, and only one of
them is a fact about the chemistry.

The announcement moved *into* `open_reachable` rather than being added to four call sites: a return
value that must be read is a rule a new caller can forget, and this one had been forgotten four
times out of four. What the function now guarantees is the operator-visible half — a WARNING naming
the connectors, and `chemclaw_connectors_unreachable_total`, counted per connector so one dark host
and a dark fleet are different rates. Callers that can reach a *human* still read the list and say
so on their own surface: the front door yields a `CapabilityDegradedEvent` before the first token,
so an answer can be marked provisional while it streams rather than retroactively; the CLI prints to
stderr, which its docstring had promised since it was written and never did.

Deliberately not an error. An unreachable connector costs its tools, not the conversation — the
obvious over-correction for a silent failure is to start raising, which would turn one dark
connector into a dead front door. The turn still answers; it just stops pretending.

`run_tool_step` gets the list too, only to make its failure legible: a missing connector's functions
are simply absent from the assembled surface, so the error blamed the template for naming a tool the
template names correctly, which sends an operator to the wrong file on a retried activity.

This is the same defect class as D-138's `ToolFailedEvent` and the two are complements: that one
covers a tool that ran and raised, this one a tool that was never offered.

### 2. Calibration pooled every calculator version (REV-12)

`connectors/calc/server/tools.py` built every `PredictionRecord` without a `calc_version`, so all of
them carried the default `""` and the unique index `(calc_type, calc_version, input_hash)`
degenerated to `(calc_type, input_hash)`. A v2 prediction upserted over v1's row — destroying the
record it existed to be compared against.

Fixed on **both** sides, because either alone changes nothing. The write path passes the running
version (`calc.pka.calc_version`, `calc.solubility.calc_version`, promoted from private helpers). The
read path gained `AND calc_version = %s` and `calibration_for` now *requires* the version rather than
defaulting it — a default would silently reproduce the pooled reading this removes, and every caller
already knows which version answered. Pooled, a version running high and one running low cancel to a
bias near zero and the pair reads as well calibrated; `calculator_trust` was one line away from
telling a chemist that.

The observation write stays version-blind on purpose: a measurement is a fact about the molecule, not
about the calculator that guessed at it, and one reported value scoring every version's prediction is
what makes a version-over-version comparison possible at all.

Dormant today (`calibration_enabled` is off), which is exactly when to fix it — before any of these
numbers is quoted to a chemist.

### 3. Two counters declared and incremented by nothing (REV-19)

`chemclaw_jobs_started_total` and `chemclaw_notes_proposed_total` sat in the declaration table and
were written by no code, so every scrape reported a flat `0`. That is worse than omitting them:
`service/metrics.py`'s gauge path already refuses to emit an unbound gauge because "a fabricated
zero would be indistinguishable from a genuinely idle service", and these two had precisely that
failure with no such protection. A PR-gate rejecting every write looked identical to a quiet
afternoon.

The note counter increments *after* the submitter returns, not before: counting the attempt would
report a healthy gate during exactly the outage the metric exists to reveal.

`agents/audit.py`'s private `_record_metric` — the lazy, tolerant import that lets a Temporal worker
record a metric without ever building `service` — was promoted to `chemclaw/metrics_bridge.py` at its
fourth caller rather than being imported across modules by its underscore name. The swallow-all is
written once on purpose: a second copy of a bare `except Exception: pass` is where a real error goes
to hide.

### What this batch did not do

Two of the six items planned were **refuted by reading the code they proposed to change**, and both
are recorded rather than quietly dropped:

- **REV-7** (job→session push-back is at-most-once) proposed yielding before marking rows consumed.
  `agents/session_events.py` documents at-most-once as a *deliberate* trade made by COR-4, replacing
  an at-least-once claim that double-delivered. The recommendation would have reintroduced the bug
  COR-4 closed. The underlying risk is real — a consumer lost between claim and delivery loses the
  notification — but the fix is a visibility-timeout redelivery, a design change to a durable path,
  not a reordering. Rewritten in `BACKLOG.md` with that shape.
- **The planned ADR duplicate guard already exists.** `tests/test_decision_log.py` has held
  `test_the_registry_has_no_duplicate_reservations` since D-109, and it goes red on exactly the bad
  merge that prompted the plan item — verified by injecting a duplicate row. The collision was caught
  by hand during a merge before CI ever ran, which is why the guard was never observed firing and was
  wrongly assumed absent.

That is five refuted leads across this review against fourteen confirmed. Each refutation had the
same shape: something that looked like a missing safeguard was a considered trade whose reasoning
lived in a docstring or a test that had not been read closely enough.
