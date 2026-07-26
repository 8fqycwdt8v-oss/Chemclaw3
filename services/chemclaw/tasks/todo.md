# Task: five reported issues across Chemclaw3 and Chemclaw3_ui

Reported 2026-07-26. Each claim verified against the code before being worked.

## Triage

| # | Repo | Issue | Verdict |
|---|------|-------|---------|
| 1 | ui | `happy-dom@^16` blocked by Replit policy | real — pin to 15.x |
| 2 | ui→**backend** | `GET /sessions`, `GET /sessions/{id}/messages` whitelisted in BFF, missing upstream | real — fix belongs in **Chemclaw3** |
| 3 | ui→backend | `GET /approvals`, `POST /approvals/{id}/decision` missing | **stale** — both exist (`service/app.py:561,591`) |
| 4 | backend | bare azide anion not caught by screener | real — SMARTS gap |
| 5 | backend | `CHEMCLAW_NOTE_REPO_DIR` undocumented in runbook | real — runbook has zero mentions |

Issue 2's whitelist entries are already annotated "Added by the companion backend
change" in `server/routes.ts` — the UI pre-registered routes the backend never grew.

## Chemclaw3 (backend)

- [x] Reproduce the azide gap (NaN3 / KN3 / NH4N3 / HN3 screen clean today)
- [x] Add a `non-carbon-azide` rule — the root cause is that every azide rule
      assumes carbon attachment, so the salt, the conjugate acid (HN3) and
      heteroatom azide reagents (TMSN3, DPPA) all fall through together
- [x] Pin the new rule in `tests/test_safety.py` + `evals/cases/hazard-rule-recall.md`
      (the table's convention: one reference molecule per rule)
- [x] Document `CHEMCLAW_NOTE_REPO_DIR` in `docs/runbook.md` — the default `.` is
      always wrong in a deployment and `git_submitter` hard-fails on it
- [x] `GET /sessions` — list the caller's sessions (new `list_for_owner` on the store)
- [x] `GET /sessions/{session_id}/messages` — read a transcript back after a reload
- [x] Update the session-scoped route inventory test (it forces this consciously)
- [x] `make lint type test` green

## Chemclaw3_ui

- [x] Pin `happy-dom` to the last 15.x
- [x] Prove vitest 4 still works on it

## Verification

- [x] backend `make lint type test`
- [x] ui `vitest run` + `tsc -b`
- [x] azide: salts/HN3/TMSN3/DPPA flag; organic azides unchanged; benign silent

## Mid-task: main was restructured and rewound

Discovered while pushing: `49cd44c` moved the tree to `services/chemclaw/` but
filled it from a snapshot of `16b63c2`, dropping everything merged in
`16b63c2..2fc903a` (21 commits) — 38 Python files, 20 test modules, 8 routes,
4 of 5 safety pair rules, 462 lines of `DECISIONS.md`. Confirmed by blob-matching
`service/app.py` to `16b63c2` and diffing all common paths (338 identical / 14 differ).

- [x] Overlay `2fc903a`'s tree onto `services/chemclaw/` (content only — layout kept)
- [x] Keep the 6 Replit-only additions; restore the `knowledge` symlink `tar` clobbered
- [x] Hand-merge the one genuinely new Replit fix (`runner.py` ISSUE-B-10 disconnect
      rollback) — `runner.py` had moved +110/−16 since the snapshot, so no clean patch
- [x] Pin that fix with a test; verified it fails when the rollback is removed
- [x] Restore CI at the repo root — Actions only reads root workflows, so `main` has
      had none since the restructure
- [x] Re-apply the five-issue fixes at the new paths
- [x] `make lint type test` green from `services/chemclaw` (924 passed, 43 skipped)

## Review

Three of the five reported issues were real in the repo they were filed against.
Issue 2 was real but filed against the UI when the missing code was the backend's,
and issue 3 read as stale — both established by reading `server/routes.ts` against
`service/app.py` rather than taking the reports at face value.

Issue 3 then changed answer underneath the task. The approvals routes *did* exist
when I checked, so the report was wrong when filed; the restructure landed mid-task
and removed them, so it is right against `main` as it stands. The restore brings
them back. Worth recording as a lesson: "already fixed" is a claim about a specific
tree, and it expires.

The azide fix went wider than the report. The reported symptom was the bare anion,
but the cause is that `organic-azide` and `acyl-azide` both require a carbon
neighbour, so one SMARTS ("azide not bonded to carbon") closes the salt, HN3 and
heteroatom-azide holes together instead of special-casing the one reported input.

An existing test had to change: `test_an_ordinary_combination_is_not_flagged`
asserted that sodium azide in MeCN raises *nothing*, which was only true because
the alert was missing. Its real claim — that swapping DCM for an acceptable solvent
clears the diazidomethane pair rule — is preserved and still tested; it now asserts
the pair rule is silent rather than the whole screen.

`GET /sessions/{id}/messages` reads through `history_provider()` with the live
session's `state`: one call path serves both stores, because MAF's in-memory
provider is stateless and keeps messages in `session.state`. No second SQL reader,
and the route is not Postgres-only.

Verified beyond the offline suite: a local Postgres 16 was stood up to exercise the
new `list_for_owner` SQL directly (the full migration chain needs pgvector >= 0.7 for
`bit_jaccard_ops` and cannot run here, so the table was created from its own
migration). Owner scoping, newest-first ordering and the NULL-owner case all hold,
and the naive `owner = NULL` form was confirmed to return nothing — the bug the
`IS NOT DISTINCT FROM` comment claims to prevent.

`make eval` reports 3 gated failures (`pharma-solvent-heavy` e_factor/pmi,
`retrieval-cross-coupling-literal-miss` recall). Confirmed pre-existing by stashing
the change and re-running: identical on the clean baseline. `hazard_flag_recall`
stays 1.0, now 11/11 with the new rule pinned.

The UI fix was also only half of issue 1. `main` there had removed `happy-dom` *and*
`vitest` as a Replit workaround, so pinning happy-dom alone would have left `npm test`
failing on a missing binary. Both are restored, and the lockfile is taken from the
npmjs-resolved side — `main`'s was regenerated behind Replit's package firewall (157
`resolved` URLs at `package-firewall.replit.local`, 116 integrity hashes differing
from the npmjs tarballs) and cannot install anywhere else.

Left deliberately undone, and flagged rather than silently skipped:

- `deploy.yml` (image build, Helm gate, credentialed rollout) is restored *in tree*
  under `services/chemclaw/.github/` but not re-enabled at the repo root. Its rollout
  job pushes to a registry with secrets; turning that back on is not a call to make
  unprompted while reconciling a regression.
- `make eval`'s three pre-existing gated failures (`pharma-solvent-heavy`
  e_factor/pmi, `retrieval-cross-coupling-literal-miss` recall) are left failing and
  kept out of the root CI gate. They predate all of this — confirmed by stashing and
  re-running on the clean baseline — and deserve their own fix rather than a check
  that is red on arrival.
- `package.json` in the UI declares `check:openapi` -> `scripts/check-openapi.mjs`,
  and that file does not exist, so the script fails for anyone who runs it.
