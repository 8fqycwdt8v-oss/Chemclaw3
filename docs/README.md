# Documentation

Which of these are true today, and which are not, is the point of the layout.

| Directory | Maintained? | What is in it |
| --- | --- | --- |
| `decisions/` | **yes** — append-only | One file per architecture decision, `D-NNN-<slug>.md`, plus the allocation ledger in its `README.md`. This is the authoritative record of *why* the system is the way it is. A merged ADR is never edited; a decision that changed gets a new ADR. |
| `planning/` | **yes** | Two living documents and no more: `BACKLOG.md` (the forty things worth doing next, deleted from the file when done) and `DEFERRED.md` (postponed, each with the reason it is not now and the trigger that would revisit it). Nothing else belongs here — see below. |
| `guides/` | **yes** | Operational how-to: the runbook, workflow versioning, the xTB catalogues, attaching a warehouse ELN or a mounted file share, and `feeder-pipelines/` — the recurring jobs that keep a corpus fresh, which run **outside** this system and whose whole contract with it is a relation (`D-2026-08-28-a-feeder-writes-a-table-and-nothing-else`). |
| `reference/` | partly | `architektur.md` — the original four-layer design. Right about the layers, **silent on connectors**, which now carry every tool, job and skill (D-118). Read it for intent, not for detail. `user-story-capability-map.md` — **maintained**: every requirement story verdicted against the code, with what serves it or what is missing. Re-audit it when a note type, connector or data source lands. `bo-capability-map.md` — **maintained**: the Bayesian-optimization layer against the use cases it serves, what BoFire ships that we do not use, and the roadmap out of the gap. Every claim it makes about BoFire's runtime behaviour is measured, so a `bofire` version bump invalidates it. |
| `archive/` | **no** | Point-in-time documents: audits, load tests, reviews, assessments, and in `plans/` the build plans whose work is finished. Accurate as of their date and deliberately not updated. Do not treat any of these as current. |

**Why `archive/plans/` exists.** A completed plan reads exactly like a current one — same
imperative voice, same ticket numbers — so leaving five of them in `planning/` beside the two
documents a session actually maintains made the directory a guess rather than a place. D-156 moved
`backlog-plan`, `connector-plan`, `foundation-plan`, `gap-closure-plan` and `parity-plan` here. They
are still cited from code, as the rationale for a seam; the ADRs remain the durable record of *why*,
and these are how it was built.

**And why three more followed on 2026-08-15.** `implementation-plan.md` and
`implementation-tickets.md` each opened by declaring themselves historical — "Stand dieses
Dokuments: **historisch**" and "Standing of this document: historical" — while sitting in the
directory this table calls maintained, and this row claimed there were four living documents when
there were six. A document that says it is out of date, in a place that says it is current, is worse
than either. Both joined `archive/plans/` along with `refactor-hardening-plan.md`, whose work closed
in `D-2026-08-03-the-refactor-closes-what-it-measured`, and
`REVIEW-2026-08-13-external-synthesis-and-gap-analysis.md` went to `archive/` as the point-in-time
review it says it is. What its findings *ask for* is in `docs/planning/BACKLOG.md`; the review itself is
a record.

**The rule the split now runs on:** `planning/` holds documents that are edited because the world
changed, and a row leaves them by being deleted. Everything that is finished, dated or descriptive
of a past state is `archive/`, and is never updated again.

**The long-form findings live in `docs/archive/findings-2026-08.md`.** `BACKLOG.md` had grown to 4,717
lines and 237 open rows across ~40 dated `Open — Left by the <review>` sections, gaining roughly
three lines for every line removed — at which size nobody read it, so nothing was closed out of it,
so it grew. The queue is now forty rows grouped by what they ask for; every finding's full
measurement, and the review that produced it, is in the archive.

For what the *code* directories are, see `ARCHITECTURE.md` at the repository root.

## A note on paths inside older documents

ADRs written before D-147 cite `DECISIONS.md`, `BACKLOG.md` and `DEFERRED.md` at the repository
root, and ones written before D-146 cite `services/chemclaw/…`. Those references are left as
written — the ADR record is append-only, and rewriting it would falsify the account of what was
true when each decision was made. The documents themselves are now `docs/decisions/`,
`docs/planning/BACKLOG.md` and `docs/planning/DEFERRED.md`.
