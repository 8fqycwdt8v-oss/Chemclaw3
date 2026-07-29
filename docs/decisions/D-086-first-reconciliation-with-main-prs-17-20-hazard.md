# D-086 — First reconciliation with `main` (PRs #17–#20): hazard screen, event sink, tool registry

**Context.** While this branch built F11, `main` merged PRs #17–#20, three commits of which solved
problems this branch had also solved, independently and differently: hazard screening (`744c265`,
D-080), `PlanEvent`/`JobStartedEvent` emission (`f2e083a`, D-077), and the `@tool` capability
registry (`76c03b2`). Merging without reconciling would have shipped two hazard screens, two
per-turn contextvar sinks, and a hardcoded tool list alongside a registry.

**Decisions — each resolved on merit, not on which side wrote it first.**

1. **Hazard screening: `main`'s `safety/` wins outright; this branch's module is deleted.** Its rule
   table is *data* (`safety/rules.yaml`) that a process-safety chemist maintains without touching
   Python, every rule carries a literature citation, and it is enforced by a `kg-validate` gate plus
   a `hazard_flag_recall` eval metric. This branch's `chemclaw/hazard.py` was a Python table with
   none of that. What was genuinely additive — four **named-substance incompatibility pairs** (azide
   salt + DCM, NaH + DMF/DMSO, peroxide + ketone, complex hydride + chlorinated solvent), each safe
   apart and dangerous together and therefore invisible to a per-substance screen — moved into
   `rules.yaml` as SMARTS pair rules. `tests/test_safety_pairs.py` pins them.

   **The azide rule earned its own comment.** Written the obvious way (the X2 form correct for an
   *organic* azide) it silently never fired on an azide **salt**, because RDKit sanitizes the anion
   to two one-coordinate nitrogens. It was caught only by screening a parsed molecule — exactly what
   `rules.yaml`'s own header instructs a contributor to do, and the same trap PR #20 recorded for
   perchlorate and permanganate. A rule that never fires is worse than no rule: it reports "no rule
   matched" for a hazard the table claims to cover.

2. **One event sink, not two.** `main`'s `agents/job_events.py` and this branch's
   `agents/turn_signals.py` are the same design (a task-local contextvar drained between streamed
   updates) with the same rationale. This branch's is a strict superset — it also carries PR-gate
   proposals and clarifying questions, and preserves their order *relative to* job launches.
   Consolidated onto it, keeping `main`'s function names as the caller-facing API so its callers and
   tests were untouched. Two sinks drained separately would have left the relative order of a
   launched job and a proposed note undefined, which is precisely what a transcript must get right.

3. **Drain ordering: signal-first, and `main`'s test assertion corrected.** A tool that ran while
   the model was producing an update ran *before* the text it then produced. `main`'s test fake
   announces its job before yielding text, so its `["token", "job_started"]` assertion reported the
   text ahead of the job that preceded it. Flipped, with the reasoning recorded at the assertion —
   the property that test names ("before the answer") holds either way.

4. **The `@tool` registry is adopted wholesale.** `main`'s `_capability_tools()` assembles from the
   registry, so this branch's 19 new tools became decorators at their definition sites and their
   modules joined the registration-side-effect import block. `agents/chemclaw_agent.py` was taken
   from `main` unchanged.

**Two inventory guards did their job.** `test_registry_holds_exactly_the_inprocess_tools` and
`test_every_session_scoped_route_is_ownership_gated` both enumerate rather than hardcode, and both
failed the moment new tools and a new session-scoped route appeared — forcing a conscious update
instead of silent drift. That pattern is worth applying to further families.

**Result.** 857 passing (41 offline skips unchanged), ruff + `mypy --strict` clean, `kg-validate` /
`skill-validate` / `prose-validate` / `eln-validate` all green — with one hazard screen, one event
sink, and one tool registry.
