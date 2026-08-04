# Documentation

Which of these are true today, and which are not, is the point of the layout.

| Directory | Maintained? | What is in it |
| --- | --- | --- |
| `decisions/` | **yes** — append-only | One file per architecture decision, `D-NNN-<slug>.md`, plus the allocation ledger in its `README.md`. This is the authoritative record of *why* the system is the way it is. A merged ADR is never edited; a decision that changed gets a new ADR. |
| `planning/` | **yes** | Four living documents and no more: `BACKLOG.md` (prioritized open items), `DEFERRED.md` (postponed, each with the reason it is not now), and `implementation-plan.md` + `implementation-tickets.md` (the build order and per-phase status `CLAUDE.md` reads). |
| `guides/` | **yes** | Operational how-to: the runbook, workflow versioning, the xTB catalogues. |
| `reference/` | partly | `architektur.md` — the original four-layer design. Right about the layers, **silent on connectors**, which now carry every tool, job and skill (D-118). Read it for intent, not for detail. `user-story-capability-map.md` — **maintained**: every requirement story verdicted against the code, with what serves it or what is missing. Re-audit it when a note type, connector or data source lands. `bo-capability-map.md` — **maintained**: the Bayesian-optimization layer against the use cases it serves, what BoFire ships that we do not use, and the roadmap out of the gap. Every claim it makes about BoFire's runtime behaviour is measured, so a `bofire` version bump invalidates it. |
| `archive/` | **no** | Point-in-time documents: audits, load tests, reviews, assessments, and in `plans/` the build plans whose work is finished. Accurate as of their date and deliberately not updated. Do not treat any of these as current. |

**Why `archive/plans/` exists.** A completed plan reads exactly like a current one — same
imperative voice, same ticket numbers — so leaving five of them in `planning/` beside the two
documents a session actually maintains made the directory a guess rather than a place. D-156 moved
`backlog-plan`, `connector-plan`, `foundation-plan`, `gap-closure-plan` and `parity-plan` here. They
are still cited from code, as the rationale for a seam; the ADRs remain the durable record of *why*,
and these are how it was built.

For what the *code* directories are, see `ARCHITECTURE.md` at the repository root.

## A note on paths inside older documents

ADRs written before D-147 cite `DECISIONS.md`, `BACKLOG.md` and `DEFERRED.md` at the repository
root, and ones written before D-146 cite `services/chemclaw/…`. Those references are left as
written — the ADR record is append-only, and rewriting it would falsify the account of what was
true when each decision was made. The documents themselves are now `docs/decisions/`,
`docs/planning/BACKLOG.md` and `docs/planning/DEFERRED.md`.
