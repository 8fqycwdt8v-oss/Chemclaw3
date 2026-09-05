# Fixing the tool-integration / storage / retrieval review — 2026-09-05

Branch `claude/tool-integration-storage-review-3zupm4` in both `Chemclaw3` and `Chemclaw3-mcp`.
The findings are in `tasks/review-2026-09-04-tool-integration-and-storage.md` (14 HIGH, 28 MED).

## Method

Eight fixers on **strictly disjoint file territories**, two waves of four. Every fixer must
reproduce its finding at `HEAD` before touching anything, and write the failing test first —
the previous session dropped several reviewer prescriptions on exactly that step.

Fixers run **only their own targeted tests**, never the full suite: the verification sweep
measured this box OOM-killing at 12.4 GB anon-rss with three suites running in a 16 GB cgroup.
The coordinator runs the full gate serially at the end. Fixers do not commit; the coordinator
commits per repo so the territories stay auditable.

## Territories

| # | Repo | Territory | Findings |
| --- | --- | --- | --- |
| T1 | mcp | `servers/calc/**`, `manifests-internal/calc/**`, MODULES.md calc rows | H1 H2 M20 M21 |
| T2 | mcp | `packages/mcp_server_kit/**`, root `tests/**` | M13 G8 G9 G11 G12 |
| T3 | mcp | `servers/{chem,rxnpredict,props,pyexec,rxnlabel}/**` | M10 M11 M14 G10 G13 |
| T4 | core | `connectors/**`, `core/mcp_session.py` | H4 H5 H6 M12 M18 M19 A8 |
| T5 | core | `agent/**` (except `research_tools.py`), `core/config/agent.py` | H7 H8 M2 M8 + plan-gate/partition |
| T6 | core | `science/calc/**`, `connectors/calc/{remote,server}`, `core/config/{store,calculators}` | M1 M3–M7 C7–C12 |
| T7 | core | `publish/**`, `durable/**`, `operations/evidence_pack.py`, `cli/validate_sinks.py` | H9 H10 H11 H12 H14 M15 M16 M17 |
| T8 | core | `science/fingerprints/**`, `retrieval/**`, `core/fulltext.py`, `ingest/eln/ingest.py`, `agent/research_tools.py` | H3 H13 M9 M22 M23 M24 |

## Steps

- [x] Wave A launched: T1, T2, T3 (mcp) + T8 (retrieval).
- [x] Wave B: T4, T5, T6, T7.
- [x] Coordinator: three `BACKLOG.md` rows, each with its trigger, for the findings no test can
      honestly close.
- [x] Cross-territory routing: nine items the fixers could not reach from inside their own
      boundaries, including the second half of the provenance fix.
- [x] `Chemclaw3-mcp` gate: **1599 passed, 7 skipped**, ruff and `mypy --strict` clean over 122
      files. `deps-audit` fails on a `transformers` CVE that predates this branch (no dependency
      changed here).
- [x] `Chemclaw3` first full gate: four failures, each fixed at its cause.
- [ ] `Chemclaw3` clean gate on the committed tree.
- [x] Four ADRs, three of them cited by name in the code that landed.
- [x] `CLAUDE.md` (the bounded-result claim the defang finding falsified), `BACKLOG.md`,
      `tasks/lessons.md`.
- [ ] PRs.

## Review

**What the method produced that a straight fix pass would not.** Requiring reproduction at `HEAD`
before touching anything paid for itself four times in eight territories, and every time the more
articulate explanation was the wrong one:

- **My own H7 did not reproduce.** `agent/tool_result_size.py:283` is
  `max(ceiling // batch_width(request), 1)` — the per-result ceiling is *shared* across a batch, not
  paid per call. The finding multiplied where the code divides, and the reviewer's own report had
  said "divided by `batch_width`" two paragraphs from where I quoted it. The conclusion survived for
  H8's reason, one layer out, so H7 and H8 were one finding with the wrong cause named.
- **M9b did not reproduce.** Text and boolean facts already fail at enqueue, via a validator written
  for a different question. A regression test now says so, because nothing did.
- **One prescription was measured and rejected.** Bounding vapour pressure at 1.5 × Tb refuses
  300 °C water, which `props` answers to +14%. 1.8 ships, with the cost stated and *why not 1.5* as
  a test rather than a comment.
- **One finding was worse than reported.** "A second chemist's provenance is dropped" was really
  "no chemist was ever recorded for any primitive" — the publish hook passed no `Publication` at
  all. A count of one and a count of zero read identically in a sentence and never in a query.

**Three fixes were declined with reasons, and those are the ones worth keeping.** Eviction defaults
stay off (turning them on makes an *upgrade* delete a chemist's Hessians). The SVG hoist would save
15 kB and make two depictions on one chat page restyle each other, a `<style>` in an inline SVG
being document-scoped. And "a tokenless bearer bundle is unusable" was built, measured to make every
shipped bundle unusable at once in test, CLI and worker processes, and reverted — a change to what a
tokenless process *is*, not a bug fix.

**What the coordinator got wrong beyond H7.** The briefs did not forbid destructive git commands,
and one fixer ran `git stash` on a tree seven others were editing. It restored cleanly. The rule now
lives in `tasks/lessons.md`; it should have been in the first brief.

**What working the review added to its own conclusions.** The review's dominant pattern was *a
control applied at every site but one*, six instances. Working it found the seventh and eighth in
the test apparatus rather than in `src/`: `tests/conftest.py` restores three cached registries under
a docstring arguing the restoring must be an invariant nothing can forget, and did not cover
`core.tool_registry` — caught by a new test failing its own precondition assertion in the full suite
while passing alone. And `app_privileges.sql` needed the new table in both directions: the gate
caught a missing INSERT, then an UPDATE nobody uses.
