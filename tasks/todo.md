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
- [ ] Wave B: T4, T5, T6, T7.
- [ ] Coordinator: the findings no fixer can close — M25/M26/M27 are "the suite cannot prove X",
      which wants a `BACKLOG.md` row with its trigger, not a test that fakes the proof.
- [ ] Full gate on both repos, serially. Fix fallout.
- [ ] ADRs for the decisions taken (each fixer that changes a default or a contract owes one).
- [ ] `CLAUDE.md`, `BACKLOG.md`, `DEFERRED.md`, `tasks/lessons.md`.
- [ ] Commit per repo, push, PRs.

## Review

(to be written when the gate is green)
