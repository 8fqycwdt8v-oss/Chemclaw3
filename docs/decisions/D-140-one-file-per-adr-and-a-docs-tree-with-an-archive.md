# D-140 — One file per ADR, and a `docs/` tree with a living half and an archive

`DECISIONS.md` was 421 KB and 134 ADRs in one append-only file, sitting at the repository root
beside `BACKLOG.md` (124 KB), `DEFERRED.md` and `ADR-REGISTRY.md`. `docs/` held a second,
near-empty ADR mechanism (`docs/adr/`, one file, describing itself as the "long-form companion"),
seven overlapping `*-plan.md` documents, and point-in-time audit reports interleaved with living
reference material. Nothing about the tree told a reader which documents were true today.

**The split, and why it is not merely tidiness.** Each ADR is now `docs/decisions/D-NNN-<slug>.md`,
with `docs/decisions/README.md` as the allocation ledger. The `D-NNN` sequence is unchanged and the
bodies are byte-identical, so all ~150 citations across the code, `BACKLOG.md` and `DEFERRED.md`
still resolve.

The reason to do it is the collision problem `CLAUDE.md` documents at length: ADR numbers had
collided three times, and the cause was structural — concurrent branches all append to the *same
last line of the same file*, each picking "the highest number I can see, plus one" against a branch
that cannot see the others. Two branches therefore pick the same number *and* conflict on the same
line. `CLAUDE.md` named the fix ("abandon the global sequence for date-plus-slug ids") and asked for
it to be raised rather than drifted into.

One file per ADR is the smaller half of that fix and keeps what the numbers are good for. The
shared append point is gone: two branches adding different ADRs touch disjoint files, and two
branches claiming the same number now collide on a **filename** — which git reports as an
add/add conflict rather than burying inside ninety lines of prose where it has been missed three
times. Date-plus-slug ids remain available if collisions somehow continue, but they cost every
existing citation, and this does not.

**`docs/adr/` is gone.** Two ADR mechanisms is one too many, and the one with a single file in it
was the one to lose. Its long-form content was folded into `docs/decisions/D-001-runtime-is-python.md`,
which is what "long-form companion" was trying to be — per-ADR files make the companion the ADR.

**The tree.** `docs/decisions/` (the record), `docs/planning/` (`BACKLOG.md`, `DEFERRED.md` and the
seven plans), `docs/guides/` (the runbook and the operational how-tos), `docs/reference/`
(`architektur.md`, historical), `docs/archive/` (audits, load tests, reviews — point-in-time
documents that are deliberately *not* maintained). The archive boundary is the load-bearing part:
`CLAUDE.md` already had to warn in prose that `architektur.md` "is historical, not current", which is
a warning that belongs in the directory layout rather than in a paragraph a reader may not reach.

The repository root now carries `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `SECURITY.md` and the
build files — the documents a newcomer should actually read first, no longer buried under a
half-megabyte of append-only history.

**Enforcement.** `tests/test_decision_log.py` was rewritten against the new shape: the filename's id
matches the file's `#` heading, ids are unique, and the index lists exactly the files present
(`RESERVED` rows exempt, as before). Its `test_the_newest_decision_is_the_last_one` is dropped —
"the tail of the file" is meaningless once there is no single file, and it was only ever a proxy for
the allocation rule that the filename collision now enforces directly.
