# D-069 — Submitter checkout ownership enforced with an OS-level advisory lock

**Context.** `GitNoteSubmitter` serializes submissions with a module-level `asyncio.Lock`, but that
lock is per-process: two processes sharing `settings.note_repo_dir` would interleave `checkout -B`
calls and silently corrupt each other's note branches. The "dedicated clone per process" rule was
documented, never enforced.

**Decision.** Every submission additionally holds an exclusive non-blocking `flock` on
`.git/chemclaw-submit.lock` inside the checkout for its full duration. A second process gets an
immediate `GitSubmitError` ("note_repo_dir is in use by another process") instead of corruption.
The lock file lives under `.git/` because `submit()` now runs `reset --hard` + `clean -fd` before
each submission (itself a fix: staged residue from a failed submission no longer leaks into the
next note's branch) — deleting a held lock file would let a new process lock a fresh inode at the
same path and break mutual exclusion. The asyncio lock stays for in-process serialization.

**Consequence.** Misconfiguration (two workers, one clone) is now a loud, actionable error, not a
data-integrity incident. flock is advisory: out-of-band git use in the clone remains outside the
contract; the kernel releases the lock if the holder dies (no stale-lock cleanup needed).

**Result.** Cross-process denial proven with a real child process holding the flock
(`tests/test_knowledge.py`); lock release after a failed submission proven too.
