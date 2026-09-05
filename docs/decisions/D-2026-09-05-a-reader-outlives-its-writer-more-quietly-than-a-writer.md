# D-2026-09-05-a-reader-outlives-its-writer-more-quietly-than-a-writer — five fresh-context reviews of the gate deletion

**Status:** accepted · **Date:** 2026-09-05

Five fresh contexts over `7654cfb` (161 files, +1,761 / −4,637), each given the diff and one
dimension, none given the account of why it was written. The code was sound in the sense that
mattered — the write path does what it says — and the review found nine defects the deletion
created or exposed. **Four of them are one shape**, and it is the shape a deletion produces that a
build does not: *a component whose producer was removed keeps its readers, and a reader with no
writer passes every test it has.* A deleted writer is loud; a stranded reader answers zero.

## The four stranded readers

**`operations.authorship` read a table nothing writes.** It answered "how much of this was
AI-written" out of `note_proposals` — every note proposed and what a human merged or rejected. The
gate is gone and no row is ever added, so on any deployment installed since it would report
`proposed=0`: not an error, not an absence, a **truthful-looking zero about the thing the manager
asked**. It now counts the knowledge writes in `audit_events`, which is where the live producer
stamps them, keyed by tool and bucketed by outcome. `tests/test_operations.py` drives it through a
seeded audit row rather than a seeded proposal, and holds the transcribed
`KNOWLEDGE_WRITE_TOOLS` against `agent.authz`'s — the one place allowed to import both.

**The evidence pack had a fifth section that could only ever be empty.** Same table, same
consequence, worse setting: the pack's own `limits` train a reader to read an empty section as
"this system recorded nothing", so a permanently dead read would have looked like a session that
wrote no knowledge. The section is deleted. What replaces it is what the pack already carried —
the `record_knowledge_note` call in `tool_calls`, and `PackJob.note_id` for a note a durable run
recorded — and the one thing genuinely lost, the id of a note written inside a turn, is named in
`LIMITS` rather than left to be noticed.

**`kg-validate`'s docstring said it gates the PR that adds notes.** It never runs between an
agent's write and its readability now; it is a CI check over a corpus. Restated as what it does
catch — a human's edit, and a systematic breakage on the next run — because "the corpus is still
gated, by the human who reviews the PR" is a claim that a control exists.

**`backfill_corpus`'s safety argument was "nothing lands in the graph unreviewed".** That was the
whole stated reason it was safe to run over a decade of documents, and it stopped being true.
The real reason is one paragraph further down and always was: it is a deterministic transcription,
one note per document, verbatim, so it infers nothing and hands a reviewer nothing to decide —
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim`, applied to a PDF. A backfill that
summarized would need a different argument, which is why it does not.

## Two defects the deletion created in the write path

**A failed push wedged the pod forever, and two docstrings in one module said otherwise.** The
writer deliberately leaves a note committed locally when the push fails — `_push` then says the
next attempt "fetches, fast-forwards past whatever landed, and pushes this commit along with its
own". It cannot: once the remote moves, the clone has diverged and `merge --ff-only` refuses, so
*every* later write on that pod raises. Measured end to end. `_replay_our_unpushed_commits` rebases
this clone's own unpushed note commits onto the remote, and refuses when any local-only commit
lacks `_RECORD_TRAILER` — which is the case the original `--ff-only`-and-raise was really
protecting, and it could not tell the two apart, so it refused both and one of them was itself.

**The knowledge-sync sidecar deleted notes the pod had just recorded.** Until this commit the
writer committed inside a private worktree and the sidecar owned the tree readers scan, so
publishing a read replica over it with `rsync -a --delete` could only remove notes that had
genuinely left the base branch. The writer commits *there* now. A note whose push failed is
committed locally and absent from the remote — the intended behaviour, asserted by a test — and the
next tick deleted it from the working tree while leaving it in the local `HEAD`, so no later
path-limited `git add` would ever restore it. Silent, permanent, and reproduced. Where there is a
writer's clone the refresh is now that clone's own `fetch` + `merge --ff-only`; the replica and its
`rsync` remain for the case they were always right for, a pod that records nothing. A divergence is
a **warning** rather than an error there, because `once` is an init container and returning
non-zero would crash-loop the pod on a stranded note.

## Three test-quality findings, and what they share

`test_pr_gate.py` was deleted with the gate, and it had been carrying assertions about code that
survived. The supporting-note count in the commit message is the clearest: that file existed partly
to pin it, its docstring names the exact mutations that survive without it, and the expression moved
from the PR body to the commit message unpinned. Also unprotected: the **retirements** half of the
write order (hoisting the loop above the subject left 148 tests green — the two order tests both
passed `dependencies` only, and the retirement test read the files into a dict keyed by id, which
discards order by construction), `record.py`'s half of the human-edit guard, the `if
outcome.written:` metric guard (every fake returned the default `written=True`), and the scoping on
the idempotence check. All five are now killed by a test, each verified by re-planting the mutation.

Three assertions were inert rather than wrong. `test_no_connector_bundle_can_reach_the_pr_gate_itself`
scanned for imports of a module *this commit deleted* and otherwise matched one call spelling, so
`import chemclaw.kg.record as r; r.record_note(...)` passed; the fast-forward test built its second
clone **after** the first write pushed, so it was never stale and removing `--ff-only` left it
green; and two assertions billed as "the absence of the mutation" checked for a note branch and a
worktree directory that nothing in the new code creates. Each is now asserted against something that
can fail.

## The rule

**A deletion's risk is not in what it removes, it is in what it leaves pointing at the removal.**
Grep for the removed *thing* and you find the writers; the readers name a table, a column or a
concept and survive the grep. The four here were all found the same way — by asking, of each
surviving reader, *who writes what this reads now* — and none of them would have been found by a
test, because a reader with no writer passes its own tests. That sentence is
`D-2026-09-05-a-reader-with-no-caller-passes-its-own-tests` one level out: that ADR was about a
component nothing calls in production, this one is about a component whose *input* nothing produces.

The prose half is the same rule applied to sentences. Nine model-facing passages still promised a
human reviewer — six skills, the `reporting` profile's live system prompt, and eight eval probes
whose `forbids_claims` had come to contradict their own `direction` (forbidding "the note is now
saved" while the direction requires saying it was). Those are corrected and verified at the emitted
artifact — every registered profile's `SystemMessage` and every bound tool's `.description` — rather
than by grep, because grep is what let them ship.
