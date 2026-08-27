# X7 false-claim repair — 2026-08-27

16 documented claims measured false (`X7-claims.md`). Every load-bearing *control* held; what is
wrong is prose. Per item: change the code to match the claim, or the claim to match the code — and
leave a gate behind, because a corrected sentence with no gate is the same defect one edit later.

## Items

- [x] F1 `CLAUDE.md` — the runaway cap is a first-party `before_model` counter, not upstream's
      `ModelCallLimitMiddleware`. **Claim-changed** (the code is right, and the same file already
      forbids that composition). Gate: absence test in `tests/test_upstream_surface.py`.
- [x] F9 `message_pairing._LANGCHAIN_SHAPE` — **code-changed**: import the one constant, so the
      comment claiming the two "cannot drift" becomes true; then soften `CLAUDE.md`. Gate:
      `tests/test_message_pairing.py`.
- [x] F3/F11 `calc`'s "five jobs" / "six read tools" — **claim-changed**, count removed. Gate:
      `tests/test_repo_map.py` derives the one-workflow claim and rejects a re-added count.
- [x] F4 "the eight validators" — **claim-changed**: nine, `sink-validate` missing. Gate: derive
      the list from `make ci`.
- [x] F5 `agent/README.md` (QM/DFT bundle, `workflows/`) and F6 `connectors/README.md`
      (`science/safety`) — **claim-changed**. Gate: package READMEs join the prose-contract corpus;
      the `science/` list and every "`x` bundle" mention pinned against the tree.
- [x] F7/F8 `BACKLOG.md` — wrong self-counts, duplicated row. **Claim-changed**; new
      `tests/test_backlog_register.py`.
- [x] F10 documents README "Five things" (seven), F12 stale `path:line` anchors, F13
      `test_layering.py` "exactly one left", F14 "three rules each enforced by a test",
      F15 `agent/challenge._default_client` (deleted module).
- [x] F16 `_tracked_directories.has_content` — **code-changed**: a package holding only
      `__init__.py` is invisible to the map guard. Gate: the constructed case, as a test.

## Verification

`uv run ruff check . && uv run ruff format --check . && uv run mypy src examples tests`, plus
`test_repo_map`, `test_decision_log`, `test_deferred_register`, `test_backlog_register`,
`test_prose_contract`, `test_message_pairing`, `test_upstream_surface`, `test_layering`,
`test_docstring_paths`.

## Review

All sixteen addressed; F2 was already fixed by a concurrent session (the conflict markers are gone,
the row kept `origin/main`'s wording, and the guard was generalised into
`tests/test_repo_map.py::test_no_tracked_text_file_carries_an_unresolved_conflict_marker`).

**Code changed, claim kept: two.** `agent/message_pairing.py` now imports `LANGCHAIN_SHAPE` instead
of restating it, so the comment claiming the two cannot drift is true and the destructive path
(`droppable_rows`) has one authority for what a row is. `tests/test_repo_map.py::_is_cache` counts
`__init__.py` as content, so a package holding only that file can no longer be invisible to both
halves of the map guard.

**Claim changed, code kept: the rest** — every one was prose describing a working system wrongly.
The counts (`calc`'s jobs, the validators, the archive's rows, this queue's rows, the read tools,
the fan-out jobs, "five things", "exactly one left") were removed rather than corrected, and each
now has a test deriving the fact from the tree.

**Found while fixing, not in the report:** `connectors/calc/connector.yaml` carried three more stale
counts ("Four jobs that are each a fan-out" over five, "these five typed jobs" twice, "the other two
sampling jobs"), and the package READMEs held nine unresolvable paths once they joined the
prose-contract corpus.

**Not gated, deliberately.** A heading may still count the items under it: measured, a mechanical
rule false-positives on four of the eight counting headings in the package READMEs (a bold
continuation line, a table, a prose enumeration), so `ingest/documents/README.md` simply lost its
number. And `path:line` anchors in the two registers are not banned: 37 rows use one, so the rule
would be a 37-row rewrite; the three stale ones now name symbols instead.

**Gate results.** `ruff check` / `ruff format --check` clean over the tree except
`tests/test_validate_connectors.py:274` (E501, a file the concurrent dead-code sweep is editing);
`mypy --strict src examples tests` → **Success, 678 files**. Targeted suites: `test_repo_map` 14,
`test_backlog_register` 4, `test_deferred_register` 4, `test_decision_log` 11, `test_layering` 73,
`test_prose_contract` 34, `test_message_pairing` 10, `test_upstream_surface` 38, `test_docstring_paths` 677 — all
green, plus
`make connector-validate` and `make prose-validate`.

The one unresolved red is `test_message_migration`'s two agent-over-Postgres tests, which **time
out** (pytest-timeout, not an assertion) on a machine carrying another session's suites at load
average 15 and 30 concurrent pytest processes: `test_erasure_reaches_turn_state_not_just_the_
transcript` passed in the 824-test run with these changes applied and timed out in the next run of
the same file. Nothing here can reach them — `message_pairing` is imported lazily by
`durable/retention.py` and by nothing the graph build touches.

# Plan-visibility + verifier-probe fixes (this session)

Findings from the Claude-Science comparison investigation, scoped to what is actionable now.

## Chemclaw3 (backend)

- [x] 1. Emit `ApprovalRequestEvent(approval_id="")` at end of a plan-gated turn whose current
      plan is non-empty and unapproved — the UI's `PlanApprovalPrompt` already mounts on exactly
      that shape (`events.py:198` documents it; nothing produced it). Helper in `api/runner.py`,
      wording constant beside the gate's other wording in `agent/plan_gate.py`. Never raises.
- [x] 2. Verifier pre-flight capability probe (BACKLOG row, `agent/verifier.py:372` bare except):
      `require_verifier_capability()` in `agent/verifier.py`, wired in `api/app.py::_lifespan`,
      fail-loudly-at-startup posture of `_require_anthropic_key`. Only when `verifier_enabled`
      and `llm_provider == "openai_compatible"` (Anthropic unaffected, per the row).
- [x] 3. Delete the BACKLOG row in the same commit (repo rule).
- [x] 4. Tests: runner emission (gated+unapproved → event before answer; approved → none;
      classic → none); probe against `_FakeOpenAiEndpoint` (compliant → pass, 400 → raise,
      prose → raise, disabled/anthropic → no-op).
- [x] 5. `make lint type test` green with Postgres up; note skip count.

## Chemclaw3_ui (frontend)

- [x] 6. Parse the `[x] `/`[ ] ` prefix the backend deliberately renders into real checkbox
      state in `PlanChecklist` (MessageList) and the `plan` trace row (TracePanel) — one shared
      helper; unprefixed lines stay plain (the GET /plan route returns bare content).
- [x] 7. Rehydrate: after `hydrateTranscript`, fetch `GET /sessions/{id}/plan` and attach the
      current plan to the last assistant message so the checklist survives a reload (silent on
      any failure — older service, no plan).
- [x] 8. Tests: prefix parsing (checked/unchecked/plain), approval card mounts from an
      `approval_request` with empty `approval_id`, rehydrated plan renders.
- [x] 9. `npm run typecheck && npm run lint && npm test` green.

## Ship

- [x] 10. PR per repo on `claude/claude-science-chemclaw-vt4vqi`, auto-merge, delete branch.

## Deliberately not done (and why)

- `harness_enabled`/`verifier_enabled` defaults stay off — deliberate deployment opt-ins,
  documented in `core/config/agent.py` / ADRs; flipping them is a deployment decision.
- Job↔plan-step linkage — tracked BACKLOG design task (needs a design, not a patch).
- Trajectory→skill distillation — standing BACKLOG row requires a measurement on a deployment
  with real sessions first; corpus measured empty 2026-08-25.

## Review

- Backend gate: `make lint` green, `make type` green, `make test` **4902 passed, 3 skipped**
  against a live migrated Postgres (dockerd + `make up` + `make db-migrate` first, per CLAUDE.md —
  the 3 skips are environment edges, not the ~216-test offline hole).
- UI gate: `tsc -b`, `eslint`, vitest **454 passed** (11 new).
- One process mistake caught mid-flight: a persisted `cd` from a compound command made the UI's
  commit land in this repo first; amended in place before the PR opened (own branch, no history
  anyone held). Lesson: `pwd` before `git commit` when two repos are in play.
- The two fixes were both "a control that existed on paper": the approval card's event was
  documented on both sides and produced by neither, and the verifier's degradation path was
  measured, written into BACKLOG, and left silent. Emitting the event / probing at startup were
  each ~40 lines; the tests are the bulk.
