# Phase 0 findings — derived mechanically, before any reviewer opinion

## F0-3 — The migration-immutability guard is vacuous on a shallow clone, and its sibling's failure message instructs you to delete a live control

- **Severity**: high
- **Location**: `tests/test_migrations_are_additive.py:344 _statements_changed_since_merge`, and the
  identical skip guard at `:428` and `:504`
- **Trigger**: run `pytest tests/test_migrations_are_additive.py` in any shallow clone whose window
  is deep enough that `git log --diff-filter=A` resolves an introducing commit for each migration —
  i.e. `git clone --depth 1` followed by any fetch that deepens it, and every Claude Code Remote
  session, which starts at a 171-commit graft.

### What the guard believes

`_statements_changed_since_merge` returns `(edited, compared)` and its docstring states the premise
the two tests rest on:

> `compared` counts only comparisons that **span a commit**: a file introduced by `HEAD` itself has
> nothing earlier to differ from, and on a truncated clone every file looks introduced by the graft
> (which *is* `HEAD`), so it is the one number telling a real run from a vacuous one.

Both tests therefore skip on `compared < 30 and is_shallow_repository == "true"`.

### What it actually does — measured

The same tree, the same commit, measured twice; only the depth of the clone differs.

| | shallow (171-commit graft) | full history (814 commits) |
|---|---|---|
| `compared` | **47** | **44** |
| `edited` | `[]` | `['002_molecule_fingerprints.sql', '003_reaction_fingerprints.sql']` |
| `test_no_merged_migration_had_its_statements_changed` | **passes** — vacuously | passes, meaningfully |
| `test_no_grandfathered_edit_outlives_its_reason` | **FAILS** | passes |

The premise is false in both directions:

1. **The graft is not `HEAD`.** A partially-truncated clone still resolves a real introducing commit
   for every migration — just the wrong one, the earliest inside the window. So the `introduced[-1]
   == head` guard never fires and every file is compared.
2. **`compared` moves the wrong way under truncation.** It is *higher* on the truncated clone (47)
   than on the complete one (44). The one number meant to tell a real run from a vacuous one **rises
   when history is lost**, so no threshold on it can ever detect truncation. `compared < 30` only
   catches the exact depth-1 case, and it stopped being reachable at all once the repository crossed
   30 migration files — there are now 47.

### Consequence

Two distinct harms, and the second is the serious one.

**The drift guard reports success having checked nothing.** On a shallow clone,
`test_no_merged_migration_had_its_statements_changed` compares each migration against its
*graft-boundary* content rather than its introducing commit. Any edit made before the graft is
invisible, so the check passes while verifying nothing about that window. A developer working
locally — which CLAUDE.md's whole workflow assumes — gets a green light from a control that did not
run. CI is unaffected (`fetch-depth: 0`), which is precisely why this has never surfaced.

**The exemption guard fails and tells you to disable a live control.** Its assertion message reads:

> The exemption has nothing left to permit, so delete it — leaving it granted means the next edit to
> that file goes unexamined.

On a shallow clone that instruction is wrong and destructive. Measured above: `002_molecule_fingerprints.sql`
and `003_reaction_fingerprints.sql` **do** still differ from their introducing commits on full
history. Both exemptions are live. Following the failure message deletes them, after which the next
real edit to either file passes unexamined — the exact outcome the exemption machinery exists to
prevent. The failure message is a trap, and it fires on the clone shape a contributor is most likely
to have.

### Evidence

```
# shallow clone (as the session starts), 171 commits
$ git rev-parse --is-shallow-repository  ->  true
edited: []
compared: 47
FAILED test_no_grandfathered_edit_outlives_its_reason
  AssertionError: grandfathered edit(s) that no longer differ from the commit that
  introduced them: ['002_molecule_fingerprints.sql', '003_reaction_fingerprints.sql']
test_no_merged_migration_had_its_statements_changed PASSED   # vacuously

# after `git fetch --unshallow`, 814 commits
$ git rev-parse --is-shallow-repository  ->  false
edited: ['002_molecule_fingerprints.sql', '003_reaction_fingerprints.sql']
compared: 44
stale exemptions: []
122 passed in 1.39s
```

### Fix

Make the sentinel the thing actually being asked about. `compared` cannot answer "is this history
complete" because it does not vary with completeness — so stop asking it to:

- Skip on `is_shallow_repository == "true"` **alone**, with no `compared` conjunct. A shallow clone
  cannot answer the question these tests ask, whatever the count says.
- Keep `assert compared >= 30` for the complete-repository case, where it still does its job of
  catching a walk that silently stopped returning results.

Both call sites at `:428` and `:504` take the same change; they already share the walk, and should
share the truncation rule for the reason the docstring gives about sharing the walk.

---

## F0-1 — Two pairs of migration files share a migration number

- **Severity**: medium (latent — no current misbehaviour)
- **Location**: `infra/sql/037_bo_suggestion_provenance.sql` + `037_document_index.sql`;
  `infra/sql/043_session_listing.sql` + `043_session_message_shape.sql`;
  ordering decided at `core/migrate.py:85 _read_sql_files` and `:174`

`_read_sql_files` reads with `sorted(sql_dir.glob("*.sql"))` and `migrate` applies in
`sorted(sources)` order, so the order within a colliding pair is settled by alphabetising the
**slug** — `bo_suggestion_provenance` before `document_index`, `session_listing` before
`session_message_shape`.

Deterministic today and the schema applies cleanly, which is why this is latent. What makes it a
finding is that nothing guards it: two branches each adding an `044_` conflict on no filename, merge
cleanly, and have their relative order decided by their slugs. If one of that pair ever depends on
the other, the dependency is settled by the alphabet, silently.
`tests/test_schema_inventory.py` checks the README inventory against the migrations and asserts
nothing about number uniqueness.

**Fix**: a test asserting migration numbers are unique. Renumbering one file of each existing pair is
only safe if its statements are byte-identical everywhere it has been applied — which the drift
guard above can confirm, once F0-3 is fixed and it actually runs.

---

## F0-2 — Three live documents state numbers the code disproves

- **Severity**: low
- `tests/README.md` and the comment in `.github/workflows/ci.yml` both state an **80%** coverage
  floor. `pyproject.toml` sets `fail_under = 84`. Measured coverage on this run: **88.31%**.
- `tests/pg.py` states "~157 tests never executed" offline. The real figure is ≤323 test functions
  across 28 files (106 `migrated_db_or_skip()` call sites).

Recorded not for its own weight but because it is the pattern this audit exists to catch: a document
asserting a fact that one `grep` disproves, left standing long enough to be believed.

---

## Not a finding: `test_reizman.py::test_bo_campaign_finds_high_yield`

This test failed on the baseline run with a 180 s timeout. It **passes in isolation in 70.26 s**.
The failure was contention — six review agents were running concurrently on this 4-CPU box while the
suite ran. Recorded here so a later reader does not re-investigate it as a defect.

Worth noting as fragility rather than a defect: a 70 s test under a 180 s cap has 2.5× headroom, and
CI runs it on a 2-core GitHub runner. It is the most likely candidate for a flake that would be
misread as one.
