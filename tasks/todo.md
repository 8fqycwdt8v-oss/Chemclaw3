# Waves 16–20 — simplification, refactoring, maintainability

Planned on measurement at `e2f090e6`, not on intuition. Every figure below is reproducible
from the scripts in each wave's brief; none of them is transcribed into prose anywhere that
a later commit could falsify without failing a test (`D-2026-09-03`).

## What the tree measures today

| Axis | Measured | Decision |
|---|---|---|
| Prose : code in `src/` | **1.28 : 1** — 75,631 prose vs 59,021 code, 452 files | W17 |
| Functions whose docstring is longer than their body | **1,110 of 3,473 (32%)** | W17 |
| Structural duplication | 33 duplicated 8-statement shapes in 8,643 windows, nearly all pydantic field blocks and SQL row mapping | **no wave — the tree is DRY** |
| Function body length | median **9**, p90 36, but 20 functions 130–315 lines | W18 |
| Worst branch count | `protocols/render.py::render_markdown` — **41 branches, 257 lines** | W18 |
| Top-level defs named exactly once in `src/` | **117**, of which **22** are named nowhere and 25 only by tests. *This row first said 144/46 and was wrong*: the scan behind it counted `tokenize` NAME tokens, and on Python 3.11 an f-string is a single `STRING` token, so every identifier interpolated into one is invisible. 27 live functions read as orphans — nine of them consecutive helpers in `ingest/eln/record.py`, which looks exactly like a dead cluster. Re-derived with `ast`. | W16 |
| `Settings` fields | **422** | W16 |
| Test tree | **179,332 lines, 384 files, 1.23× `src`**, full run 23:42 | W19 |
| `__init__.py` re-exports | 49 | fine |

**The premise this plan rejects.** "Simplify" usually means deduplicate and shorten functions.
Measured, neither is this tree's problem: duplication is negligible and the median function is
nine lines. What is large is everything *around* the code — the prose that must be kept true, the
tests that must be kept passing, and the settings that must be kept reachable. So these waves go
after the maintenance *surface*, not the line count.

**The constraint every wave inherits from waves 10–15.** A deletion is a change in behaviour until
proven otherwise, and this tree has already been burned by both directions: it deleted 1,442 lines
of unreachable specialist code correctly (`D-2026-08-15`), and it kept `reject_widening` alive for
months as "a claim that a control exists". Nothing gets deleted on a name-count alone.

**That constraint earned itself inside this plan's first hour.** The dead-code row above was wrong
in the direction that would have deleted live code, and the audit's own near-miss was reporting the
egress guard's arming function as dead because it is imported under an alias
(`from chemclaw.core.netguard import arm_from_settings as arm_egress_guard`). Two independent
blind spots, both in the reassuring direction, before a line was removed.

---

## W16 — Dead code, and the repo's own rules that nothing tests

`CLAUDE.md` states three rules as non-negotiable and tests none of them: *"No abstraction without a
second real caller (Rule of Three); an abstraction with one caller gets inlined"*, *"No boilerplate:
only code that is actually used"*, *"Delete dead params, empty interfaces, and 'for later' stubs on
sight"*.

- [ ] W16.1 Decorator-aware reachability. The 144 single-reference defs include MCP `@tool`s,
      Temporal `@workflow.defn`s and pydantic validators that are reached by *registration*, not by
      name. Build the reachability set from the registries themselves, not from a token scan.
- [ ] W16.2 The 46 named nowhere — classify each: registered, test-only, or genuinely dead.
      `evals/autonomy.py` alone holds four (205 lines); `BACKLOG.md` already carries a row saying
      `turn_cost_ratio` "scores a fixture, not the system".
- [ ] W16.3 Settings with no reader. 422 fields; find every one nothing in `src/` reads, and every
      one no deployment file can reach. This exact defect shipped before (`D-2026-08-11`: three
      compaction settings with no reader, beside a config comment in the present tense).
- [ ] W16.4 Dead parameters and single-caller abstractions, per the Rule of Three.
- [ ] W16.5 Make the rules checkable where a machine can see them, with each guard watched refusing.

## W17 — The prose load: 1.28 : 1, and which half is a liability

**This wave does not delete docstrings.** This tree's prose is deliberate and load-bearing — it
records *why*, and waves 10–15 depended on it. But 75,631 lines is 75,631 lines that must be kept
true, and those waves found ~100 present-tense claims that were false.

- [ ] W17.1 Measure where the ~100 stale claims waves 10–15 corrected actually *were*: module
      docstring, function docstring, or inline comment? Per 1,000 lines of each. That ratio is the
      evidence for any policy, and nobody has computed it.
- [ ] W17.2 Classify the 1,110 doc-longer-than-body functions: prose that **records a decision**
      (load-bearing — keep), prose that **restates the code** (a liability — it drifts, and the code
      is the better copy), prose that **belongs in an ADR** (move it, cite it).
- [ ] W17.3 Find prose that is already a duplicate of an ADR it cites — the same argument in two
      places, only one of which is allowed to be edited.
- [ ] W17.4 Propose the rule, and enforce what a machine can see of it.
- [ ] W17.5 Act only where the classification is unambiguous. Anything arguable stays and is listed.

## W18 — The complexity hotspots, where change is riskiest

Twenty functions carry 130–315 line bodies. These are the places a future change is most likely to
break something, and the median function being nine lines is what makes them stand out rather than
excuse them.

- [ ] W18.1 `protocols/render.py::render_markdown` — 41 branches, 257 lines.
- [ ] W18.2 `api/runner.py::run_turn` (315) and `api/routes/turns.py::post_message` (308).
- [ ] W18.3 `connectors/calc/activities.py::_dispatch` (289 lines, 25 branches) and
      `publish/dialect.py::rows_for` (276, 18).
- [ ] W18.4 Every refactor proven behaviour-identical against the base, by measurement rather than
      by argument — the bar `CLAUDE.md` sets ("diff behavior between the base and your change").
- [ ] W18.5 Refactor only where the split is genuinely clearer. A 300-line function that is one
      honest dispatch table is not improved by scattering it.

## W19 — The test tree's own maintainability

179,332 lines against 145,730 of source, and 23:42 to run. Nothing has ever asked what that buys.

- [ ] W19.1 Where the 23:42 actually goes: the slowest 1% of tests, and whether the cost is
      structural (container setup, migrations per test) or a handful of slow cases.
- [ ] W19.2 Redundant coverage: tests that exercise the same behaviour through the same path.
- [ ] W19.3 Fixture sprawl and the six test files over 2,000 lines (`test_deploy_chart.py` is 5,205).
- [ ] W19.4 The test tree's own prose is 0.60 : 1 — 57,346 lines. Same question as W17, asked of it.
- [ ] W19.5 Act where it is safe; a test deleted is coverage deleted, so the bar is that the
      behaviour is still covered somewhere, proven by driving it.

## W20 — Did the simplification remove a control?

Wave 15's finding was that this review's own guards needed auditing. A *simplification* wave is
precisely where a control gets deleted by accident — so this wave turns the lens on 16–19 rather
than opening a new axis.

- [ ] W20.1 Every deletion in W16–W19, re-examined against the question "what would now go
      unnoticed?"
- [ ] W20.2 Mutation-test the guards W16–W19 added, on wave 15's method: each must be watched
      refusing its own defect.
- [ ] W20.3 Re-derive every number this plan and those waves wrote into prose, at HEAD.
- [ ] W20.4 Full gate, ADR, PR, merge.

---

## Review

### The ratio that three measurements disagreed about

`src/` prose:code was measured three times this wave, independently, and came back **1.16**,
**1.28** and **1.49 : 1**. A 28% spread on the number the whole of W17 was planned around.

The tree did not change between them. The *method* did — specifically how each classified a
blank line inside a docstring, and a continuation line of a multi-line statement. None of the
three stated its method beside its number, which is why the disagreement was invisible until
someone put them side by side.

Settled with one stated rule, reproducible from `tasks/todo.md` itself: every physical line of
every `.py` under `src/chemclaw` lands in exactly one bucket — DOCSTRING if it falls inside the
line span of a bare string-literal expression statement, else COMMENT if its stripped form starts
with `#`, else BLANK if empty after stripping, else CODE. The four sum to the file's line count.

| | lines | code | doc | comment | blank | prose:code |
|---|---|---|---|---|---|---|
| `src/chemclaw` | 145,949 | 59,030 | 53,992 | 21,848 | 11,079 | **1.28 : 1** |
| `tests` | 179,999 | 96,126 | 49,849 | 7,758 | 26,266 | **0.60 : 1** |

test code : source code = **1.63 : 1**.

**This is the wave's own defect, one level up from the one it went looking for.** W17 corrected
eight present-tense counts in docstrings for not stating how they were counted. The ratio that
justified opening W17 had the same problem, and it took three disagreeing measurements to notice.
A number without its method is not a measurement; it is a claim that someone measured.

What survives unchanged: `src/` carries more than twice the prose density of `tests/` under every
one of the three methods. The prose burden is in the source, not in the suite — which is the
opposite of what a review looking for bloated test prose expects to find, and the reason W19's
recommendation is *do not trim test prose*.

### Per-wave outcomes

Recorded as **premise → what measurement said**, because in five of six the premise lost. The
full argument is `D-2026-09-11-the-debt-was-in-the-claims-not-in-the-code`.

| wave | premise | measured |
|---|---|---|
| W16 dead code | 46 defs named nowhere | **1 dead definition in 3,133** (`DesignListing`, 6 lines) |
| W16 settings | unread knobs among 422 | 2 declared-only, both read by `entrypoint.sh` and parity-tested |
| W16 Rule of Three | 45% single-caller | not a defect rate; **net ~109 lines**, and 3 of the audit's own recommendations did not survive re-derivation |
| W17 prose load | 1.28 : 1 is the liability | **98.9% DECISION, 1.1% restatement**; the ratio is not the finding |
| W17 prose gate | 21,391 unchecked refs → build a gate | **declined at 82.9% false positives**; two narrow rules instead, 7.5% → 7.6% |
| W18 complexity | 20 functions need splitting | **1 split of 20**; 13 had already had their extractable parts extracted |
| W19 test tree | 179,332 lines is too many | ratio fine, 1 duplicate in 5,747, 1.6% restating prose; runtime concentrated — slowest 25 are 512 s of 1,552 s, BO files 389 s (25%) |

**What was actually wrong, in none of those categories**: three of four HTTP surfaces unbounded;
two metric series named as alert targets that nothing emits; a settings field documented for a
subsystem that does not exist (found 2026-08-16, never fixed); 14 dangling symbol references; nine
parametrised tables that could not say why they passed, one proven vacuous.

### What this plan got wrong about itself

Three times, all in the reassuring direction:

1. **The dead-code figure.** 144/46 was really 117/22 — the scan counted `tokenize` NAME tokens and
   an f-string is one `STRING` token on 3.11, so 27 live functions read as orphans.
2. **The prose ratio.** Measured three times independently: 1.16, 1.28, 1.49 : 1. None stated its
   method. Settled above.
3. **The duration profile.** Declared invalid and an agent told to skip the question; it had been
   read while still being written. The agent re-checked rather than complying, and was right.
4. **The profile that replaced it.** Its "31 BO tests are 43.5% of the run" was measured while four
   other pytest processes shared the database; GP fitting is CPU-bound, so contention inflated it
   about threefold. Clean: BO files are 25%, and the fixture the figure motivated is worth 12% of
   one file and under 1% of the suite. It reached an ADR draft, this file and a PR body first.

Each was caught by something other than the session that made it — two by subagents re-deriving
rather than accepting, one by putting three numbers side by side. That is the argument for the
fan-out shape, and it is worth more than any line this wave deleted.
