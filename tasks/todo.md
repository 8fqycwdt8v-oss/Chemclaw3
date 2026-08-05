# Task: deep review and refactor of knowledge management (layer 4)

Branch: `claude/knowledge-management-refactor-348v3d`. Decision:
`docs/decisions/D-2026-08-05-three-searches-that-disagreed-about-one-note.md`.

**The brief was open — "deep review, code refactoring and improvement of knowledge management" —
so the first output was a map rather than a diff.** What it found was five problems with one
shape: a rule written in two places, with a docstring in one of them asserting that the other
agreed. Two were behavioural defects nobody had reported; three were open [M] findings in
`docs/planning/BACKLOG.md`; the rest was the duplication that produced all of them.

_(The previous occupant of this file was the agentic-engine review (#128,
`D-2026-08-05-one-rule-in-three-places-is-three-rules`), which landed on `main` while this branch
was in flight; before that, the tool/skill seam (#129) and the live-test lane (#124/#127). All are
in `git log`.)_

---

## Plan (three commits on one branch, each independently green)

### Commit 1 — one definition per rule
- [x] `chemclaw.kg.search`: one `search_text` / `query_terms` / `term_coverage`, replacing three
      haystacks; all four readers repointed (`find_notes`, `GraphRetriever`, the note index, the
      digest)
- [x] `Note.outgoing_relations`: the frontmatter form wins, so declared edge metadata reaches a
      query
- [x] `TemporalWindow` base; one `scan_notes_dir`; one `dangling_links`; one `_registry_problems`;
      `note_relative_path`; `note_id_for_reaction` at the last inline site
- [x] Observability: unparseable notes logged + counted; `proposal.py` gains `exc_info`;
      `conflicts.py`'s narrowing `assert` becomes structural
- [x] `proposal_reason_chars` and `graph_analytics_top_n` into config; `propose_note` split
- [x] `redact_secrets` before the truncation that was described as if it were redaction

### Commit 2 — the PR-gate stops touching the shared checkout
- [x] Probe first: git 2.43 permits a worktree inside `.git/` (the design had a fallback if not)
- [x] Submit in `.git/chemclaw-worktrees/<branch>`; delete `_checkout_clean`/`_return_to_base`
- [x] Migration repair for a parked checkout; leftover sweep (prune alone does not do it)
- [x] `_require_dedicated_checkout`'s reason replaced, not retired; both locks kept, one for a new
      reason
- [x] `reindex_notes` busts the cache before it reads
- [x] `tests/test_pr_gate_read_window.py` **rewritten**, not sign-flipped; prose corrected in the
      runbook, the sidecar, the live bootstrap, the config field and the test docstrings

### Commit 3 — a proposal record that can be replayed
- [x] `kg/submission.py` (forced: `pr_gate` imports `proposal`, so `proposal` cannot import back)
- [x] `NoteProposal.dependencies` + `infra/sql/036`; `content_hash` deliberately unmoved
- [x] `proposal_store` reads rows by name — a column inserted mid-list is exactly the hazard
- [x] The two false docstrings corrected; `GET /proposals/{id}` returns the whole unit

### The record
- [x] ADR + ledger row; four backlog rows deleted (not struck through); the stale 37/ten/fourteen
      counts corrected; `kg/README.md`'s absolute given its condition and its unwired code named

---

## Review

**What was measured, not argued.** Two scripts, run before and after:

| | before | after |
|---|---|---|
| notes matching `reaction` in the agent's haystack and no other | 5 | 0 |
| notes findable by their own `compound_smiles` in one reader and not another | 14 | 0 |
| declared edge confidences reaching a query | `None`, `None` | `0.5`, `0.8` |
| declared edge `valid_from` reaching a query | `None` | `2025-06-30` |
| gold retrieval recall / precision | 4x1.0 + 1x0.5 / 5x1.0 | unchanged |
| a credential-bearing git failure vs the 300-char "bound" | 118 chars, stored verbatim | redacted |

The eval numbers are the reason the *union* of the three haystacks was safe to take: widening the
retriever cost nothing on the gold set, so the fallback (narrowing `find_notes` instead) was never
needed. Had precision moved, the goal was one definition — not a particular one.

**What the tests caught that the plan did not.** `tests/test_logging.py` — the test forbidding an
import on the logging path — failed the first time the new redaction ran: a `\1`-style replacement
template is compiled lazily by `re`, and that compilation imports `re`. An import from inside a
filter once wedged a Temporal worker, which is why the rule exists; a callable replacement avoids
it. The test was written for a defect that had already happened and caught a second instance of it
immediately.

**Where the plan was wrong.** It said to reuse `core/db.py::_redact` for git URLs; that function
round-trips a DSN through libpq's own parser and does not apply to a git remote. The redaction was
built in `core/logging.py` instead, beside the inventory that already existed.

**What was deliberately not done.** `graph.related` and the two `crosslink` reverse lookups have no
production caller and were kept — each is the only read path for a capability a merged ADR claims,
and deleting one deletes the claim. They are named as declared-but-unwired in the package README
rather than left for the next reader to guess about.

**Skips, named.** `tests/test_note_proposals_postgres.py` (4 tests) skips here: the sandbox's
pgvector predates migration 013's `bit_jaccard_ops`. Removing the skip was attempted — a local
Postgres 16 was built and pgvector installed — and when that still fell short the new code path was
measured directly against it with `027` + `036` applied by hand. The other 135 skips are the
standing offline set (Temporal test server, `xtb`/`crest` binaries).
