# Task: deep review and refactor of knowledge management (layer 4)

Branch: `claude/knowledge-management-refactor-348v3d`. Decision:
`docs/decisions/D-2026-08-05-three-searches-that-disagreed-about-one-note.md`.

**The brief was open — "deep review, code refactoring and improvement of knowledge management" —
so the first output was a map rather than a diff.** What it found was five problems with one
shape: a rule written in two places, with a docstring in one of them asserting that the other
agreed. Two were behavioural defects nobody had reported; three were open [M] findings in
`docs/planning/BACKLOG.md`; the rest was the duplication that produced all of them.

_(Three reviews landed on `main` while this branch was in flight and each held this file in turn:
the database-integration review (#131), the BO ceiling (#130) and the agentic engine (#128). All
are in `git log`, and their decisions are in `docs/decisions/`.)_

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

## Measured, and not defects

Recorded so they are not re-litigated:

- **SSE polling against the pool.** 200 streams ÷ `session_event_poll_seconds=2.0` = 100 borrows/s
  per front-door process, each a sub-millisecond indexed `SELECT` on
  `session_events_unconsumed_idx` ≈ 0.1 connection-seconds/s. The pool is not the constraint; the
  event loop is, which is D-119's original finding.
- **The audit chain's global advisory mutex.** Every append across the fleet serializes on
  `pg_advisory_xact_lock(0x43484D4157_00_01)` for ~4 round trips, so the ceiling is a few hundred
  appends/s deployment-wide — far above current demand, and correct by design, since a forked chain
  cannot be repaired. A ceiling worth stating, not a defect worth fixing.
- **The SQL surface itself.** Every application statement binds its values; the four sites that
  interpolate an *identifier* are each guarded (a closed `_PRUNABLE` map, `table.isidentifier()`,
  an int from config, a validated identifier regex), and the one un-parameterized surface — a
  warehouse binding's `where:` — is a documented operator-authored trust boundary.

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

---

# Task: the front door's leak, and a comment that broke migrations everywhere (2026-08-05)

Branch `claude/temporal-workflows-llm-testing-5nziyp`, rebuilt on `main` after three parallel
sessions (#130–#132) landed the worktree fix, the multi-file proposal and the redaction
independently. What follows is what was left.

- [x] **1. The [H] RSS leak, named and fixed** — `chemclaw.cli.leak_probe`, `make leak-probe`.
- [x] **2. The migration drift guard** — a comment could not change what a migration did, and it
      should not be able to break one. Plus the 031 revert and an immutability test.
- [x] **3. `refresh` reporting a lost claim**, the webhook contract, the layering invariant.
- [x] **4. The mutant walk** — eight behavioural survivors killed in the two leading files.

---

## Review — what was actually measured

**The leak's cause was not the shape the soak suggested.** The soak said "steepening", which reads
as an unbounded structure filling; the cause was `configure_telemetry()` **returning early**. With
no meter provider set the OpenTelemetry API proxies every instrument call and retains the proxy
forever, so a turn with telemetry *off* leaked 35 `_ProxyMeter`s, 35 `_ProxyHistogram`s, 70 locks
and 35 lists. Measured in-process against the real front door: **+20.7 KB and +178 live objects per
turn before, +2.7 KB and +3.3 after.**

**The hypothesis I went in with was wrong, and refuting it took one line.** MAF re-entering
unconnected tools into the agent's process-lifetime exit stack was plausible and documented in this
repo's own docstring; the soak's `chemclaw_connectors_unreachable_total` sat flat at 0, which was
evidence against it, so the probe counted those callbacks first: flat at zero across 200 turns.
Killing the plausible explanation before building on it is what left the search pointed where the
answer was.

**A comment broke migrations everywhere and CI could not see it.** `031_bo_campaigns.sql` was edited
on `main` — comments only, and the comment was true — and the byte-level drift guard then refused to
migrate any database that had already applied it. CI always starts from an empty database. Second
occurrence: `006_audit_events.sql`, failing the same way for four days.

**Three sessions did the same review in parallel, and mine was not always the better answer.** The
worktree placement I chose (a sibling of `knowledge_dir`) is worse than the one that landed (under
`.git/`), the plan-emission helper that landed holds its own state where mine passed it, and the
redaction that landed reuses `redact_secrets` and a config value where mine wrote a local regex.
Rebuilding the branch on `main` and re-applying only what was genuinely missing cost an hour and was
the right call: shipping the worse duplicate of a GxP control would have been much more expensive.

**Left open, deliberately:** the 600 s heartbeat/detection coupling, and the string-mutation
survivors that a substring `pytest.raises(match=...)` cannot kill.
