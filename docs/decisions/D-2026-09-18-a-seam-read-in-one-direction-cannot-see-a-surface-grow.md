# D-2026-09-18-a-seam-read-in-one-direction-cannot-see-a-surface-grow — the two `calc` tools nothing here calls are declined, and now say so

**Status:** accepted · **Date:** 2026-09-18

## Context

`Chemclaw3-mcp`'s backlog carried a row opened on 2026-09-14: *"Eight `calc` tools are hardcoded in
a third module neither repository checks."* It reported that
`tests/test_sibling_manifest_agreement.py` held a `_CALLERS` tuple naming two modules —
`connectors/calc/compose.py` and `remote.py`, 13 sites over 10 tools — while
`connectors/calc/server/tools.py` held 11 more sites naming 10 tools, 8 of them watched by nothing.
It named the fix as *"that repository's `_CALLERS` tuple"*.

**Every arithmetic claim in that row was right and its prescription was already spent.**
`D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names` had replaced the tuple
with `_callers()`, which derives the caller set from who imports a dispatcher out of
`chemclaw.connectors.calc.remote`. Re-measured on this commit: **4 modules, 26 sites, 18 distinct
tool names** — `bo/calculators.py` as well, which the row did not know about — and all eight of the
tools it listed as unwatched are watched.

What the row also said, and asked to be confirmed rather than assumed, is the part that was still
open: checked and unchecked together name 18 of the 20 tools `servers/calc/tool-surface.json`
records, and `optimize_geometry` and `predict_logd` are named by **no** hardcoded dict-literal site
in any of them. Confirmed, and the reason is structural in both cases — measured on this commit
against the fleet checkout, not read off a comment:

* **`optimize_geometry` derives the same cache key as `relax_structure`.** The fleet's
  `identity.COMPUTE_TOOLS` routes both through `_from_spec` with an `OptSpec`, and
  `XtbSpec.cache_key` does not carry the tool name. Driven for `CCO`, `optimize_geometry` and
  `relax_structure` on the structure `optimization_inputs` embeds from it produce one identical
  string, `xtb.opt@…:389b625b3220108a:5e9dada5819590e9`. The two return different payloads — a
  summary without coordinates, and the full result with them — so caching either under that key
  poisons the other. `connectors/calc/server/tools.py::optimize_geometry` composes
  `embed_structure` plus `relax_structure` instead, and its own comment says so.
* **`predict_logd` has no key at all.** Driven, `calculation_key` answers it with `calc_key=None`
  and a caveat naming the pKa to key instead, and `cached_remote` raises `CalcToolError` on a
  keyless tool rather than recomputing forever. The composite is assembled here from a cached
  `predict_pka` plus a local Crippen sum.

So neither is dead and neither is reached by a shape the walker cannot see. They are declined, for
reasons that were written into two code comments and into no assertion.

**The general defect is that the seam was read in one direction.** "Every name this repository
sends is one the fleet serves" catches a rename. It is silent about the other thing a tool surface
does — grow. A tool the fleet adds that nothing here calls is either a capability this repository
is missing or a duplicate of something it already composes, and both are decisions somebody should
take; today they arrive as silence, which is how these two arrived.

## Decision

Add the reverse direction, derived, and reconcile it against a table of declined tools with a
reason each.

`test_every_calc_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason` subtracts what
`_hardcoded_calls()` derives from what `tool-surface.json` publishes and asserts the remainder
equals `_DECLINED`'s keys. No count is written anywhere: both sides are derived and the table is
whatever is left over.

**`_DECLINED` is a table and `_CALLERS` was a list, and the difference is that this one can be
reconciled.** The tuple had nothing on the other side of it — a ninth caller module was simply
*absent*, and absence is what no assertion can see, which is why it went two modules and 13 sites
stale while its own docstring claimed every hardcoded call. A set of declined *tools* is the
opposite shape: the fleet publishes what it serves and the walker derives what is called, so the
table is subtracted from a derived difference in both directions on every run. It cannot gain a
stale row, lose a needed one, or outlive its subject. What it holds is the one thing no machine can
derive — *why* — and that is exactly the thing worth writing down.

Deriving the declined set instead of tabling it was considered and is not available: "which tools
are deliberately not called" has no observable in either tree. The choice is between a reconciled
table and no record, and the row this closes exists because there was no record.

The older test about the two composites this repository assembles carried a half asserting that
`predict_logd` is served. That half is now subsumed — a withdrawal fails the new test with the
reason string it invalidated — so it keeps only the `compute_thermochemistry` invariant and is
renamed for what it now says. Checked before renaming: no merged ADR cited the old name, and
`tests/test_decision_log.py::test_every_test_an_adr_names_still_exists` is what says so rather than
a grep, because it red on this file's own first draft when that draft quoted the retired name.

## Consequences

A 21st tool in the fleet's `calc` surface fails this repository's suite in the pull request that
syncs the checkouts, with a message naming the two ways out. The reasons the two composites are
assembled client-side are now enforced rather than commented, and each fails loudly if the fleet
changes the premise it rests on.

**A cross-repository row can describe work already done, and nothing on either side says so.**
This one named a fix — "that repository's `_CALLERS` tuple" — that had stopped existing four days
before it was read, because the repository that could close it is not the repository that holds the
row, and `Chemclaw3-mcp`'s own register is explicit that a row about another repository is one
nothing there can check. That is the register working as designed rather than a defect in it: the
row survived precisely because it was the only thing keeping the question alive, and the question it
was keeping alive turned out to be the half it could not name. The lesson is for whoever works one
next — re-measure the row against the other tree before implementing its prescription, because the
numbers in it are about the afternoon it was written and the *fix* in it is about a commit that has
since moved.

**One residue is measured and left open.** `connectors/calc/server/tools.py::_CALIBRATED` maps a
property name to a fleet tool name — `predict_solubility` and `predict_pka` — and reaches the wire
through `remote_version`, which is not in `_DISPATCHERS` and whose tool argument is a tuple-unpacked
local rather than a literal. Both names are already covered by other sites, so nothing is unchecked
today; a third calibrated row naming a tool the fleet does not serve would not be. Teaching
`_literal_strings` to resolve a value out of a specific module's table would put one module's
private data structure inside a generic walker, so it is a backlog row rather than a change here.

## What keeps it true

- `tests/test_sibling_manifest_agreement.py::test_every_calc_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason`
  — driven red three ways against a scratch copy of the fleet's `tool-surface.json`, each reverted:
  adding a served tool nothing here calls, removing `predict_logd` from the served surface, and
  renaming a live call site in `connectors/bo/calculators.py` to `optimize_geometry` so a declined
  tool becomes a called one.
- `tests/test_sibling_manifest_agreement.py::test_the_composite_this_repository_assembles_is_not_also_served_by_the_fleet`
  — the `compute_thermochemistry` invariant, which is about a tool that is *not* served and which
  the derived test cannot express.
- `tests/test_sibling_manifest_agreement.py::test_the_calc_seam_calls_only_tools_the_fleet_records_serving`
  — the other direction, unchanged, and still the only place an unresolvable tool expression fails.
- `tests/conftest.py::_report_sibling_skips` — every sibling-reading check is opt-in on a fleet
  checkout, so "green" and "ran" stay different statements. (Amended 2026-09-19: this read "all
  four", which resolved to no set — the section lists three tests plus this reporter, which is not
  itself opt-in, while the file holds five `_sibling_or_skip()`-gated tests. A count, not the
  decision, so it is corrected here rather than superseded.)
