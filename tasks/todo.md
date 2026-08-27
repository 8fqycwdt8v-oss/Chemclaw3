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
