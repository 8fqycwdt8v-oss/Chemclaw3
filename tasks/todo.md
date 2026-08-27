# Knowledge-system review: implement all findings (2026-08-27)

The plan for implementing `docs/archive/REVIEW-2026-08-27-knowledge-system-analysis.md` in full,
as one branch (`claude/knowledge-system-analysis-3xwwc3`) of ordered, per-theme commits.

## Work packages (all done)

- [x] WP1 — relation directions: `RELATION_SIGNATURES`, `validate()` enforcement, corpus edges
      re-authored compound-side, `test_seed_corpus` un-pins the inversion
- [x] WP2 — note schema: `extra="forbid"` on nested models, directory-matches-type check,
      surrogate walk into `conditions`, malformed-target naming
- [x] WP3 — corpus content fixes, `valid_from` dates, `knowledge/README.md` rewrite
- [x] WP4 — kg core: conflict scan rewritten output-sensitive (3111 ms → 11 ms at 4k dated
      notes), `_LAST_SCAN` stamped after the cache write, list copies out of the TTL cache,
      per-directory index locks
- [x] WP5 — report renderer: partial-failure sections render evidence plus the incomplete marker
- [x] WP6 — retrieval: `search_text` covers conditions/source, GraphRetriever ranks before the
      cut, `query_terms` floor honored in the fallback
- [x] WP7 — silent-zero class: `RetrieverSkip` third channel, `EvidenceSweep.sources` /
      `sources_skipped`, `note_reindex_effective` derivation, `find_notes` widening +
      `total_matches`, embedding batch chunking + `note_embedding_key`
- [x] WP8 — PR-gate honesty (`D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed`):
      gate-commit trailer + foreign-tip refusal, `GitRemoteError` retryable class, one open row
      per note (`superseded`, migration 058), `SubmissionOutcome`, dependencies never overwrite,
      cross-pod advisory lock, `make proposals-reconcile`
- [x] WP9 — memory loop (`D-2026-08-27-a-retirement-rides-its-replacement`): `SynthesisUnit`
      pairing, real `superseded-by` edge, partial-read retirement skip, store-seeded promotion
      dedup, truthful promotion summary, `make synthesize`
- [x] WP10 — grounding + existence: `groundable_ids` (document citations ground), `calc_refs`
      existence checked against the calculation store in `kg-validate`
- [x] WP11 — hygiene: warehouse-retriever tests hermetic (10 hard failures offline → 0), seed
      corpus stops citing calculations no store holds, BACKLOG rows closed/narrowed, ADRs +
      ledger, full gate, PR + auto-merge

## Review

- Verification ran with the infrastructure up (dockerd + `make up` + `make db-migrate`), so the
  Postgres- and Temporal-backed slices genuinely ran rather than skipping.
- `make lint`, `make type` (src + tests, 681 files) and the suite are green; every validator
  except `helm-validate` runs green locally — helm is not installed in this sandbox (documented
  live edge; the chart is untouched by this branch).
- Two flaky tests observed are pre-existing and unrelated (verified via `git stash`):
  `test_deploy_chart.py::test_the_fleet_ceiling_…` (order-dependent) and
  `test_connector_transport.py::test_a_bundles_startup_report_…` (timing under load).
- The new `calc_refs` existence gate immediately caught the seed corpus citing fabricated keys —
  the gate finding a real instance of the defect class it was built for; the corpus now states
  in prose why its refs are empty, which is the discipline a real note is held to.

---

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

# The three deferred items — plan approved 2026-08-27

Step 0 probed the environment's credential: **live** (the 2026-08-25 401 is gone), so Track A
takes the live-measurement path.

## Track B — job↔plan-step linkage (this repo)

- [x] ADR `D-2026-08-27-a-job-names-the-step-it-serves` + ledger row
- [x] `core/plan_context.py` — ambient `(plan_step, plan_hash)`, the `session_context` pattern
- [x] `agent/plan_link.py` — `stamp_plan_link` middleware (first `in_progress` todo +
      `plan_identity`), attached in `langgraph_agent.tool_governance_middleware` whenever the
      harness runs (innermost, inside the gate)
- [x] `ConnectorJobInput`/`JobRecord`/`JobRecordSummary` gain `plan_step`(+`plan_hash`); store
      columns + migration `057_job_plan_step.sql` (additive, defaulted)
- [x] `JobSignal`/`record_job_started` fold the ambient step in; `JobStartedEvent.plan_step`;
      `graph_stream` maps it
- [x] Tests: `tests/test_plan_link.py` (7), launch stamp + empty stamp in
      `test_connector_jobs.py`, Postgres round-trip + listing in `test_job_record_postgres.py`
- [x] BACKLOG §3 row deleted in this change
- [ ] `make lint type test` green, PR, auto-merge

## Track B UI — plan checklist job chips (Chemclaw3_ui)

- [ ] `shared/events.ts` `JobStartedEvent.plan_step` + helpers builder default
- [ ] chatStore job feed carries `planStep`; `PlanItems` badges the matching row
      (spinner running, ✓/✕ on completion/failure via job_id join)
- [ ] Tests; typecheck/lint/vitest green; PR, auto-merge

## Track A — verifier opt-in + judge margin (this repo)

- [ ] Chart/runbook opt-in surface (commented `CHEMCLAW_VERIFIER_*` in values.yaml naming the
      startup probe + the reproducibility caveat) + values-prose pin in `test_helm_chart.py`
- [ ] `infra/live` margin measurement: re-roll flagged answers 3×, measure flip rate/margin near
      threshold 0.7
- [ ] Hysteresis band from the measured width (`verifier_review_band`, re-roll majority inside the
      band only) + `chemclaw_verifier_band_rerolls_total`; ADR; delete the DEFERRED
      reproducibility row in the same commit
- [ ] Correct the stale BACKLOG §5 "API-KEY is present and rejected" row (re-measured live
      2026-08-27)

## Track C — trajectory census instrument (this repo)

- [ ] ADR defining "recurring trajectory" + the trigger numbers that would greenlight the
      distillation generator (which stays unbuilt until a real corpus exists)
- [ ] `chemclaw.cli.trajectory_census` + `make trajectory-census`; offline tests with fixture rows
- [ ] Delete the duplicate "Memory records…" BACKLOG row; point the surviving row at the
      instrument

## Deliberately not done (user-confirmed 2026-08-27)

- Code defaults for `harness_enabled`/`verifier_enabled` stay `False` — the chart is the opt-in
  surface (harness already on there).
- No explicit `plan_step` tool argument — rejected in the ADR with its reopen condition.
- No distillation generator, no synthetic corpus — the ADR defines the greenlight numbers.
