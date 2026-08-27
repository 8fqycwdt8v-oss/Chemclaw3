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
