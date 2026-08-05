# D-2026-08-05-three-searches-that-disagreed-about-one-note — Three searches that disagreed about one note, and a gate that borrowed the tree it guards

**Status:** accepted · **Date:** 2026-08-05

## Context

A deep review of layer 4 — the Markdown knowledge graph, its indexer, and its PR-gate. It found
two behavioural defects nobody had reported, closed three open [M] findings, and removed the
duplication that produced all of them. What the five have in common is worth naming, because it is
also the thing this repository keeps rediscovering: **every one of them was a rule written down in
two places, with a docstring in one place asserting that the other agreed.**

## Decision

### 1. A note has one searchable text, and it lives in layer 4

There were three haystacks. `agent.graph_tools.find_notes` searched `id + type + compound_smiles +
tags + body`. `retrieval.vector_index.note_text` — read by `GraphRetriever`, by the dense
embedding, and by the lexical tsvector — searched `id + tags + body`. `durable.digest._matches`
built a third by untyped `getattr` on `object` and matched the whole query as one phrase. Each of
the three carried a docstring claiming agreement: `note_text`'s said "one definition … cannot
drift", the digest's said "matching mirrors `find_notes`".

Measured against the committed 38-note corpus: **5 notes** matched the term `reaction` in the
agent's haystack and in no other; **14 notes** were findable by their own `compound_smiles` in one
and not the other. The consequence is the one that reaches a chemist — `find_notes` hands the model
a note that `gather_evidence`, and therefore every report section, cannot then cite. In a
transcript that is indistinguishable from the note not existing.

`chemclaw.kg.search` is now the one definition: `search_text(note)`, `query_terms(query)`,
`term_coverage(note, terms)`. All four readers call it. Both drift counts are zero afterwards.

**The union won, not the intersection**, and that was checked rather than argued: a note's type and
its structure are things a chemist searches *for*, and the retriever was the leg that could not see
them. The gold retrieval set is unchanged either way — recall 4×1.0 + 1×0.5, precision 5×1.0,
before and after — so widening cost nothing measurable and the fallback (narrowing `find_notes`
instead) was not needed.

Whether *all* terms must match stays with the caller: `find_notes` and the digest require all,
`GraphRetriever` keeps its widening fallback. That is a ranking policy, not a definition of text.

**One migration consequence, stated because it is invisible otherwise.** `note_index.fingerprint`
is a stat signature, so it detects a changed *note* and structurally cannot detect a changed
*definition of a note's text*: an incremental `make reindex` finds nothing to do and the stored
embeddings go on describing the old text forever. Run `make reindex-full` once on upgrade
(`docs/guides/runbook.md` §(vi-b)).

### 2. The frontmatter form of an edge wins over the body form

`Note.outgoing_relations` deduplicated `(rel, to)` with the **body** form winning. A body
`[[rel:target]]` can express a relation and nothing else; a frontmatter entry can express the same
relation plus a confidence and a validity window. So deduplicating in favour of the body discarded
the only information one of the two forms carried — silently, at parse time.

It was not a corner case. All three notes in the shipped corpus that declare a typed relation also
write the link in the body, so the measured effect was that **D-134's edge metadata existed in the
schema, in the parser and in the corpus, and reached no query**: `Relation.confidence` read as
`None` everywhere, and `related(..., as_of=)` had no dated edge to filter. Declared 0.5 → `None`,
0.8 → `None`, `valid_from: 2025-06-30` → `None`.

The richer declaration now wins, the corpus's exemplars work without being edited, and
`tests/test_seed_corpus.py` asserts the corpus carries at least one edge confidence and one edge
window a reader can actually see — so they cannot go inert again unnoticed.

### 3. The PR-gate stops borrowing the tree everyone reads from

`GitNoteSubmitter` ran `git checkout -B note/<id>` in the shared `note_repo_dir`, which is also
where every reader resolves `settings.knowledge_path`. While a submission was in flight an
unreviewed agent-authored note was readable *as knowledge* by `find_notes`, `gather_evidence`, the
digest and the ELN sync — and, because `load_notes` caches, for up to `graph_cache_ttl_seconds`
after the branch was gone. Nothing filtered it: `created_by == "agent"` is read in one place, to
label a report chunk, and no reader consults `note_proposals.state`. A crash was the same defect
with no bound: `_return_to_base` ran from a `finally`, which SIGKILL does not.

The submission now happens in a `git worktree` under `.git/` — beside the submit lock, for the same
reason: no reader's `rglob` reaches it, `git clean -fd` does not touch it, the sync sidecar's
`rsync --delete` does not write it.

**Neither finding could have been closed by restoring the tree more carefully.** An exposure that
lasts as long as the tree is switched needs the switch to stop happening. That is also why the
regression target is `git reflog show HEAD` containing no `note/` entry: a submitter that switched
and switched back would satisfy every before/after assertion.

Three supporting decisions:

- **A parked checkout is repaired before the worktree is created.** Without it this change
  *entrenches* the finding it closes — nothing else would move a parked tree back, and
  `worktree add -B` then fails for exactly the note whose submission crashed. `checkout -f`, not
  `reset --hard` + `clean -fd`, so the sidecar's untracked published notes survive; restricted to
  the `note/` prefix so an operator's branch is never touched.
- **Leftover worktrees are swept at the start of every submit.** `git worktree prune` does *not*
  do this: it reclaims metadata whose directory has vanished, on a three-month expiry, so against
  a SIGKILLed submission that left both it is a no-op.
- **Not `--no-checkout`.** It would make a submission cost one file write instead of materializing
  the corpus, and it would silently void `_contained_note_path`, which defends by resolving
  symlinks in the tree *as materialized*. `tests/test_knowledge.py::test_symlinked_directory_on_base_is_refused`
  is the veto. Worth revisiting only with a different containment check, never by weakening this one.

Both locks stay, and one of them for a new reason: the sweep removes every worktree under the
shared root, which is safe only because no sibling submission can own one.
`_require_dedicated_checkout` stays too, with its justification **replaced rather than retired** —
it no longer protects uncommitted work from a `reset --hard`; it stops the gate force-pushing an
agent-authored note to the ChemClaw source remote, which `note_repo_dir`'s `"."` default still
makes reachable.

### 4. A proposal record keeps the whole unit, not its first file

`propose_note` stored `content=files[0].content`. A submission is one reviewable unit — a note and
the notes its links depend on, which is why `NoteSubmission` carries a list (D-133) — so the record
kept a fraction of it while two places asserted otherwise: `kg/proposal.py` ("a `FAILED` row can be
replayed because the bytes it would have written are still here") and `api/routes/proposals.py`
("the note exactly as it would land in the tree"). Replaying a `job-result` proposal would have
written a note whose `[[wikilink]]` to its `compound` dangled — failing `kg-validate` on the PR it
reopened — and a reviewer approved a link whose far end was off screen.

`NoteProposal.dependencies` (JSONB, `infra/sql/036`) carries the rest, and `GET /proposals/{id}`
returns it.

**`content_hash` deliberately still covers the subject note alone.** The row records *this version
of this note*; hashing the whole file set would make every pre-existing row look like changed
content and append a second row asserting "the note changed" — a claim about history, in the one
table whose purpose is to be a record of it. `dependencies` refreshes on an unchanged re-proposal
like `reference` and the provenance columns.

### 5. Truncation is not redaction

`pr_gate._REASON_CHARS = 300` was justified as keeping "a token-bearing remote URL" out of
`note_proposals.reason`, a compliance table `chemclaw.durable.retention` deliberately never prunes.
A realistic credential-bearing git failure measures **118 characters**, so the bound had never once
applied to the case it named. `core.logging.redact_secrets` — the redaction the log filter already
applied, extended to cover URL userinfo — now runs *before* the cut, and the cut is
`proposal_reason_chars` in config.

### 6. The duplication that produced all of it

One `TemporalWindow` base, replacing an interval validator and an `is_current` written twice. One
`scan_notes_dir`, replacing four copies of the glob-and-stat walk. One `dangling_links`, replacing
the validator's and analytics' implementations. One `_registry_problems`, replacing two identical
comprehensions and the `Path('?')` sentinel they used for exactly the notes a reader would most
want located. One `note_relative_path`, so the `<type>/<id>.md` layout is not re-spelled in a
reader. `note_id_for_reaction` at the one call site that still spelled it out. Two cheap subset
tests pinning `_DISTILLED_TYPES` and `_CONFLICTING_RELATIONS` to the registries they must stay
inside — a typo in either disabled a feature in the direction of "clean".

And an unparseable note is now logged and counted (`chemclaw_notes_unparseable_total`) rather than
dropped in silence. The old argument — `kg-validate` reports it — is true of the repository and not
of the tree a pod is serving, where a partial sync leaves a deployment retrieving less than it
should with no signal anywhere.

## Consequences

**What this does not fix, said out loud rather than left for the worktree change to imply.** No
reader can still distinguish a proposal from merged knowledge: `created_by` is read in one place,
to label a chunk, and `note_proposals.state` is consulted by nothing. This removes the *exposure*;
it does not add a filter. The sidecar's own `rsync --delete` window stays open. Post-merge
staleness is still bounded by `graph_cache_ttl_seconds`, by design (DA-5). Concurrency is
unchanged — still one submitting process per `note_repo_dir` — and worktrees buy isolation from
readers, not throughput; the `git_submitter` docstring that rejected them as over-engineering *for
concurrency* was right and stays right.

New costs. Every submission materializes the corpus into a fresh worktree (I/O and transient
space), and a crash leaves one full copy under `.git/` until the next submission sweeps it — no
time-based GC. A leftover worktree still holds the unreviewed bytes on disk under `.git/`, where no
ChemClaw reader looks but a filesystem backup would; if the compliance posture requires "the bytes
do not exist outside the branch", this is not that. `worktree add` is a new failure surface:
submissions can now fail for reasons that did not exist before. And the sweep's safety now rests on
the `flock`, so on a filesystem that does not honour it the blast radius grows from two interleaved
branches to a live worktree deleted mid-submission (`docs/guides/runbook.md`).

**On the tests.** `tests/test_pr_gate_read_window.py` was rewritten rather than sign-flipped: three
of its four tests drove raw `git checkout -B` by hand and asserted a property of *git*, so against
the fixed submitter they would have gone green while proving nothing — the exact failure that file
was written to prevent. Every test now drives `GitNoteSubmitter`, samples the shared tree between
every git command, and pairs each absence with a positive check that the submission really
happened. Each was confirmed to fail under a mutation that removes what it pins, as were the four
new search tests.

**What was kept rather than deleted.** `graph.related` and `crosslink.calc_ref_index` /
`notes_for_calculation` have no production caller, and each is the only read path for a capability
a merged ADR claims (D-134 typed-edge queries, STO-7 calculation crosslinks). Deleting one deletes
the claim with it, so they are recorded as declared-but-unwired in `src/chemclaw/kg/README.md`
instead.

**Corrections to the record.** `kg/README.md` stated "retrieval is graph traversal, not top-k
vector similarity" as an absolute; that is the default (`retrieval_mode`), not the whole capability
set, since D-062. `DEFERRED.md` and `BACKLOG.md` said the corpus is 37 notes over ten types and
fourteen relations; it is 38, eleven and fifteen. Four backlog rows are deleted, not struck
through: the read window, the crash-parked checkout, the multi-file replay gap, and
`_REASON_CHARS`.

**Verification.** `make lint type test` green (3,257 passed). `make kg-validate`, `make eval`
(0 regressions), `make prose-validate`. The Postgres half of commit 3 skips in this sandbox — the
available pgvector predates migration 013's `bit_jaccard_ops` — and was measured instead against a
real Postgres 16 with `027` + `036` applied by hand: the JSONB round-trip, the UPSERT refresh onto
the same row, and every field mapping through all four statements.
