# Deployment-readiness review and hardening — 2026-09-04

Branch `claude/code-review-hardening-lchtz5`. Goal: prove the tree is deployable, robust and
maintainable, and fix what proves it is not. Every finding is a claim about a commit, so every
finding carries a command and its output or it does not ship.

## Method

Fourteen fresh-context reviews, each with its own scope and no shared belief about the tree, over a
running stack (`dockerd` + `make up` + `make db-migrate`) so the Postgres-backed half of the suite
is evidence rather than a skip.

Six cross-cutting sweeps: deployment surface, security, concurrency and resource safety, failure
modes, observability, and dead code / prose-vs-code drift.
Eight per-package correctness reviews: `agent/`, `core/`, `api/`, `durable/`,
`connectors/`+`protocols/`, `ingest/`, `science/`+`retrieval/`+`memory/`, and the
`publish/`+`deliver/`+`kg/`+`operations/`+`templates/`+`cli/`+`evals/` remainder.

## Steps

- [ ] Baseline `make lint type test` green, with the skip count named.
- [ ] Wave 1 — fan out the fourteen reviews.
- [ ] Wave 2 — aggregate, dedupe, re-verify every HIGH/MEDIUM against `HEAD`; drop what does not
      reproduce. A finding two reviewers agree on is still one measurement until it is run.
- [ ] Wave 3 — fix each confirmed defect at its root, with a test that fails before and passes after.
- [ ] Wave 4 — ADRs for anything that changes behaviour, registers updated, full `make ci`, PR, merge.

## Review

(filled in at the end)
