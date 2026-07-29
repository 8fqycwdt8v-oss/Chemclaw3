# Documentation

Which of these are true today, and which are not, is the point of the layout.

| Directory | Maintained? | What is in it |
| --- | --- | --- |
| `decisions/` | **yes** — append-only | One file per architecture decision, `D-NNN-<slug>.md`, plus the allocation ledger in its `README.md`. This is the authoritative record of *why* the system is the way it is. A merged ADR is never edited; a decision that changed gets a new ADR. |
| `planning/` | **yes** | `BACKLOG.md` (prioritized open items), `DEFERRED.md` (postponed, each with the reason), and the build plans and ticket lists. |
| `guides/` | **yes** | Operational how-to: the runbook, workflow versioning, the xTB catalogues. |
| `reference/` | partly | `architektur.md` — the original four-layer design. Right about the layers, **silent on connectors**, which now carry every tool, job and skill (D-118). Read it for intent, not for detail. |
| `archive/` | **no** | Point-in-time documents: audits, load tests, reviews, assessments. Accurate as of their date and deliberately not updated. Do not treat any of these as current. |

For what the *code* directories are, see `ARCHITECTURE.md` at the repository root.

## A note on paths inside older documents

ADRs written before D-147 cite `DECISIONS.md`, `BACKLOG.md` and `DEFERRED.md` at the repository
root, and ones written before D-146 cite `services/chemclaw/…`. Those references are left as
written — the ADR record is append-only, and rewriting it would falsify the account of what was
true when each decision was made. The documents themselves are now `docs/decisions/`,
`docs/planning/BACKLOG.md` and `docs/planning/DEFERRED.md`.
