# D-2026-08-17-a-queue-row-is-a-claim-and-claims-go-stale — a backlog row is a claim about the code, and claims go stale

**Status:** accepted · **Date:** 2026-08-17

## Context

Both registers carry rules about their own hygiene — delete a row in the commit that closes it,
never append a status note, name an anchor so any row can be checked with one `grep`. Both rules
were being followed. Neither prevents the failure this pass found.

Every anchor in both files was opened against `HEAD`. The result:

- **Four `BACKLOG` rows described code that no longer exists or was already fixed.** The mid-turn
  resume's dropped `user_input_requests` names a field of the deleted agent framework: the string
  has **zero** occurrences under `src/` and survives only in `tests/fakes.py` and
  `tests/fakes_langgraph.py`. The row's `api/runner.py:780` lands, in a 931-line file, inside the
  docstring of `_turn_store` (`api/runner.py:771`), which is about which gates decide the
  deployment's durable memory store — nothing to do with resuming a turn. (An earlier draft of this
  ADR called it a citation past the end of a 774-line file. That was true of one commit and of no
  other; a line number is a claim with a shorter shelf life than the row that carries it, which is
  the finding rather than an aside.) The failed-durable-job
  row's cited line is now *inside the comment documenting its fix*. The `adelete_thread` row argues
  that `CHECKPOINT_TABLES` is hand-maintained and could silently miss a new upstream table, which
  `tests/test_message_migration.py:577`
  (`test_the_erased_table_list_is_derived_from_upstream_not_asserted_against_itself`) has asserted
  against upstream's own `base.MIGRATIONS` since before the row was written.
- **Eight were misstated in a way that sends a reader to the wrong function.** The write-gate row
  says the built-in gate "never consults the connector-declared `state_changing` set";
  `agent/authz.py:154` does exactly that in `side_effecting_tools()`, and `DEFAULT_WRITE_TOOL_GATES`
  is a deliberately narrower RBAC default-deny set with a test pinning it as a subset. Acting on
  that row would have widened a gate while believing it was closing a hole.
- **Two stated the opposite of what the tree does.** One says a data-subject erasure request "has no
  route across the seven tables" — `agent/leaver.py:161` erases across **twelve** in one
  transaction (`len(_ERASE)`; seven literals plus the checkpointer's three and the store's two),
  with a dry-run default and per-table rowcounts, shipped as `make user-erase`. One points at a
  `DEFERRED.md` row that does not exist and never did.
- **Three carried their own deferral trigger** ("restart when upstream emits usage per content
  block") and were therefore `DEFERRED.md` rows filed in the queue of things to do next.

`DEFERRED.md` was in better shape — no row was closable and none was wholly dead, which is what a
register maintained through three architectural moves looks like. But several were arguing from
evidence that no longer holds, and two of those reasons were *measurably* false rather than merely
dated: the DoE row refused a capability because it "needs cyipopt + SCIP" when SCIP already ships
transitively and cyipopt is optional behind a `scipy.minimize` fallback, and the JS row claimed
`node --check` coverage that exists nowhere in the repository.

The commit that rewrote `DEFERRED.md` says it corrected six rows. **That number is a diff, and this
file is the wrong place to assert one.** The standard both registers set is a claim one `grep`
settles, and no `grep` over `DEFERRED.md` recovers how many rows a commit touched — a later audit
counted seven rewritten and four added and could not be reconciled against the sentence here without
`git log`, which is where a diff belongs. What the file itself answers is that it holds 44 rows
(`grep -c '^| \*\*' docs/planning/DEFERRED.md`), and which of them name the date they were
re-measured.

## Decision

**A row's anchor is checked before the row is worked, and a wrong row is corrected or deleted as
the contribution.** That is now written into `BACKLOG.md`'s header, next to the rules it already had.

Three consequences follow, and they are the part worth keeping:

**1. A note addressed to a future implementer is not a queue item.** `graph_stream._from_update`
silently drops `__interrupt__` because an interrupt arrives as a tuple. Nothing raises one today, so
the branch is unreachable and untestable, and the row's own stated purpose was "so whoever adds the
first interrupt finds it". A queue capped at what a person can hold is the wrong place to leave that
message; the line that drops it is the right one. It is now a comment there, and the row is gone.

**2. A row whose trigger is an upstream release belongs in `DEFERRED.md`, even when the work was
already done once.** The `stream_events(version="v3")` migration was built, measured and reverted,
and its row instructed the reader not to restart it. A queue of "the next thing worth doing" should
not contain an entry telling you not to do it.

**3. A deferral resting on a false reason is worse than no deferral**, because nobody re-examines
it. The BoFire refusal had stood on a dependency cost for months; measured, both criteria return a
design on a constrained continuous domain with cyipopt absent. The refusal is still right — no story
asks for an optimality criterion — but it now rests on a reason someone can argue with, and the same
false claim has been corrected in `docs/reference/bo-capability-map.md`, which had copied it.

## Consequences

`BACKLOG.md` holds 30 rows (`grep -c '^- \[ \]' docs/planning/BACKLOG.md`) — this pass's output
plus the ten its header records as arriving from concurrent reviews while it ran. Of the rows it
removed: ten were closed by this session's commits, four deleted as obsolete, three moved to
`DEFERRED.md`, and one became a code comment. Eight more were corrected in place, with the
measurement that changes the work, and stayed.

**The first draft of this section stated that as before-and-after counts, and the arithmetic did not
close.** 10 + 4 + 3 + 1 is eighteen removals against a claimed drop from forty rows to twenty, so at
least two removals went unrecorded — and neither the forty nor the twenty was recoverable from the
file. It also gave a line count, which is the one figure in this document guaranteed to go false:
the next edit to `BACKLOG.md` falsifies it, and the first one did. A row count survives a row being
reworded; a line count does not. So what is asserted here is what a `grep` at `HEAD` returns, the
itemisation is left as the incomplete account it is rather than padded to fit, and the before-state
is `git log`'s to answer — the same division of labour both registers already state for closed rows.

Surviving rows carry their corrections: the solvate collapse also collides knowledge-graph note ids
(worse than filed) while leaving the D-011 calculation cache untouched (better than feared); the
attachment-parse row is `[L]` rather than `[M]` because the only real fix is a killable subprocess;
the secrets row is five read sites in four modules rather than fifty-seven, because the hazard it
described is already closed by the redaction filter and only the three credentials are left.

**What this does not fix.** Nothing here is enforced by a test, and it deliberately is not — a
machine cannot tell a stale claim from a live one without opening the code, which is the work
itself. `tests/test_deferred_register.py` and `tests/test_decision_log.py` continue to check the
shapes a machine *can* see, and one of them caught a truncated ADR id in this very pass. The rest is
a reading habit, and the header now asks for it explicitly.

The same pass found three claims outside these registers that a test *can* hold, so those became
tests rather than prose: the runbook named a `trivy` gate that runs nowhere
(`test_every_supply_chain_gate_the_runbook_names_actually_runs`), `tblite` shipped to every pod while
no module may import it (`test_the_xtb_engine_is_not_in_the_runtime_closure`), and `Structure`
normalization read a setting that shapes a *remote* cache key
(`test_no_setting_shapes_the_bytes_the_server_hashes`). Where a document makes a checkable promise,
the promise becomes a test — which is the standing rule this pass supplies three more instances of.

**Say what such a test holds, and no more — and expect the holding to move.** The first of the three
was written as a substring search over `image.yml` for the backticked gate names in the runbook's
table. Both halves of that were defeatable: a workflow *comment* naming the tool satisfied the
search, and prose outside the table was not read at all, so the section could claim in a sentence
what it was forbidden to claim in a row. An audit defeated both while the test stayed green, and
both were closed the same day — the workflow is now parsed as YAML with only `uses`/`run` counted
and shell comments stripped, and the section's sentences are read alongside its table.

Two things follow. A summary of a test belongs at the level of the guarantee ("no supply-chain tool
this section names may be one nothing executes"), because that survives the mechanism being
rewritten under it, and the `BACKLOG.md` row summarising this one had to be corrected twice in a day
for saying otherwise. And a test is a claim about the code with exactly the same shelf life as a
backlog row — which is this ADR's thesis, arriving from the direction it did not expect.
