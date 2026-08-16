# Deep review of the LangGraph migration, the GxP removal and the tool exodus

Range reviewed: `7336f1d..84148a1` — 569 files, +53,599 / −33,521. Four parallel review agents
(agent core · GxP/persistence · science+connectors after the exodus · front door/evals), each
required to verify by reading code and running it rather than by reading docstrings.

## Baseline, measured before any change

Docker, Postgres and Temporal started first, because a local `pytest` without them skips ~157
Postgres tests and still prints green.

| | result |
|---|---|
| `pytest` (infra up) | **3978 passed, 0 skipped, 0 failed** |
| `ruff check` / `ruff format --check` | clean, 590 files |
| `mypy --strict` | clean, 590 files |

**Every defect below is invisible to that suite.** That is the finding behind the findings: the gate
was green throughout, so nothing here is a regression the tooling could have caught — each one is a
gap between what a declaration claims and what the code does.

## Fixed and pushed (each proven by reproduction)

- [x] **CRITICAL — the grant reconciliation stripped write access to every LangGraph table on the
      second deploy.** `REVOKE ALL ON ALL TABLES` reaches the six tables `setup()` creates; the
      enumerated re-grants named none. Reproduced in Postgres 16: the app role *owns* `checkpoints`
      and still ends with `INSERT=f SELECT=t`. First install survives (tables absent), the second
      `helm upgrade` takes every turn down at its first checkpoint write. The guard was blind —
      `_tables()` knew only `infra/sql/*.sql`, so `_DYNAMIC`'s explicit `checkpoints` entry was
      discarded. ADR `D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name`.
- [x] **HIGH — a dry run could write a durable memory.** `write_file`/`edit_file` are registered by
      middleware, so they are in no part of `side_effecting_tools()` and neither the dry-run refusal
      nor the plan gate saw them. Fixed with a path-aware predicate rather than by adding the names:
      one verb serves `/memories/` (Postgres, outlives everything) and `/scratch/` (turn-local), and
      gating the name would deny an unapproved turn the notepad it needs to produce a plan.
- [x] **HIGH — a template step read a tool's refusal as its answer.** Measured: on the plain-args
      invocation this path used, a failed MCP tool returns a bare `str`, so the error sentence became
      `${steps.<id>.result}` *and* `audit._recording` booked the refused call as `ok`. Invoking with
      the whole call was tried first and rejected — it stringifies a `job` step's dict payload.

Suite after the three: **3988 passed**, lint and `mypy --strict` clean.

## Open, highest consequence first

Not fixed in this pass. Each names an anchor so it can be checked with one `grep`.

### The calc wire — the physics left and the seam around it was not finished
- [ ] `CALCULATION_EPOCH` reaches **no** calc cache key. `CalculationKey.build` has one caller left
      (`connectors/qm/cache.py:96`, DFT); `remote.py:166` builds the key field-by-field from the
      server. Three places — including `tests/test_calc_payload_schemas.py:144`'s own failure
      message — prescribe bumping it as the remedy for a changed payload meaning. It does nothing.
- [ ] `connectors/calc/remote.py:92` passes no `read_timeout_seconds`, so the only live bound is
      httpx's un-overridden `sse_read_timeout` of **300 s**, not `calc_server_timeout_seconds=900`.
      `registry.py:71-87` records from measurement that this is the bound that raises and that the
      other is swallowed *silently*. A CREST search past 300 s never returns while `beating` keeps
      heartbeating, so Temporal sees health and the job burns its full 4 h.
- [ ] Nothing in `deploy/` points anything at the calculation server: no `CALC_SERVER`, no
      `CALC_TOKEN`, no `8860`, no egress rule. `helm install` yields a `calc` bundle whose every tool
      and all five durable jobs fail against loopback. `values.yaml:200` still calls it the pod that
      runs the binaries; `deploy/Containerfile:85-101` still installs xTB into every image.
- [ ] `settings.xtb_geometry_decimals` still shapes half of every **remote** key —
      `Structure._normalize_and_validate` rounds before the structure crosses the wire.
- [ ] `list_artifacts` / `fetch_artifact` read a store with no writer left; `ArtifactStore.put` has
      no caller. The eviction schedule, eight settings and `019_artifact_store.sql` go with it.
- [ ] `connectors/calc/remote.py:91-107` — the blanket `except` spans the `yield`, so a **Postgres**
      failure is reported to the chemist as "the calculation service is not answering", retryable.
- [ ] `compose.py:738` stamps `method=settings.xtb_method` onto `ReactionEnergyResult` — a local
      string describing a calculation this process did not run — and drops the server's `engine`.
- [ ] `calc_session` is monkeypatched wholesale by every test, so the function holding the three
      findings above **is never executed as written**. This is why a green suite cannot see them.
- [ ] No live lane can start: `processes.sh:47` requires connectors, chem/safety/calc are never
      started, and `make live-e2e-full-stack` starts only `props` and `rxnpredict`.

### The front door
- [ ] Every failed tool call emits **both** `tool_failed` and `tool_result`, and the error text joins
      `ToolCallTrace.outputs` — the corpus `score_answer` grades against. `graph_stream.py:241` gates
      on a `status` that `tool_authz.py:183` unconditionally rewrites to `"success"`; that function's
      own docstring names `graph_stream` as the reader that had to change, and it never did.
      Measured across four failure shapes on a real compiled graph.
- [ ] `api/static/app.js:114` appends `token` events with no `evt.agent` check, splicing a
      subagent's prose into the answer bubble — and `case "answer"` only fills an *empty* element, so
      the clean `AnswerEvent.text` never replaces it. The server side is correct.
- [ ] `graph_stream.py:151` drops `namespace` for `updates`, so a subagent's tool calls, results and
      **plan** are emitted as the main agent's. With the harness on, the helper's `write_todos`
      replaces the supervisor's `PlanEvent` — under `plan_only`, that is the checklist a chemist
      approves.
- [ ] `runner.py:402` has no `return` after the `empty_answer` error, so the turn also yields an
      empty `AnswerEvent`, spends a judge call scoring `""`, and books `completed=True`.
- [ ] `retrieval/fanout.py:100` — a leg that *raised* reports `chunks: 0`, identical to one that
      found nothing, and `chemclaw_evidence_source_failures_total` carries no `source` label while
      the chunks counter does. This is `D-2026-08-01-a-cap-that-starves-a-source` again.

### Persistence
- [ ] `agent/leaver.py` never touches `tool_result_links` / `tool_result_blobs`, which hold the full
      text of every tool result, keyed by session. `tests/test_leaver.py` derives completeness from
      columns whose *name* identifies a person, so a session-keyed content table is invisible to it.
- [ ] `message_migration.py:242` is a destructive in-place `UPDATE`; its own docstring and
      `043_session_message_shape.sql` both promise the original stays readable. It runs as a Helm
      **pre-upgrade** hook, so it rewrites the data the *previous* release is still serving with a
      reader that raises `TypeError` on the new shape — and `helm rollback` stays broken. Needs an
      ADR, not a patch.
- [ ] `scratchpad.py:74` claims retention prunes the memory tables; `_PRUNABLE` contains no store
      table. `store_vectors` is never created (no `index_config`), so one erasure statement is a
      permanent no-op and the FK comment beside it describes a constraint that does not exist.
- [ ] `retention.py:138` — the `LIMIT` bounds the deletes, not the scan; the `GROUP BY … HAVING
      max(...)` aggregates the whole table on an unindexed expression under `statement_timeout`.
- [ ] `session_store.py:84` — only the MAF branch is guarded, and the fallback at `:96` always
      returns `AIMessage`, so a chemist's own question can render as something the agent said.

### The dead surfaces the removals left
- [ ] ~24 calculator settings (`xtb_*`, `crest_*`, `pka_*`, `solubility_rmse_log`) have no reader in
      `src/`. Found independently three times. The seven pKa/solubility calibration constants are the
      dangerous subset: the *server* bakes those values into `calc_version`, so editing them here
      changes nothing while `.env.example:223` presents them as the predictor's calibration.
- [ ] `core/turn_signals.record_handoff` has zero callers anywhere; `set_current_specialist` /
      `reset_current_specialist` have only test callers, so `agent/audit.py:310` writes an `agent`
      column that can never be non-empty — `D-2026-08-10`'s "records the specialist beside the human"
      is false in the data. `close_memory_store()` has no caller either.
- [ ] `tblite` is a runtime dependency with no importer, kept alive by one test — and `ALPB_SOLVENTS`,
      the launch gate for four durable jobs, is derived against *this* checkout's copy rather than
      the server's.

### Record drift
- [ ] `ARCHITECTURE.md:35` lists "the specialist team", which `CLAUDE.md:57` says does not exist.
- [ ] `api/events.py:413` and `graph_stream.py:211,337` say `agent/team.running_specialist` raises
      the handoff, in the present tense, thirty lines from `events.py:422` saying nothing produces it.
- [ ] `science/README.md:3`, `science/__init__.py:3`, `connectors/README.md:21,34` still call
      `science/calc` "the physics" and list the deleted `science/safety`.
- [ ] `CLAUDE.md:132` names three `BACKLOG.md` rows that do not exist; one was declined in this same
      range and one has shipped. `BACKLOG.md:283` is a closed `[x]` row against the delete-on-close
      rule. `BACKLOG.md:229` says erasure has no route; it has one, over eleven tables.
- [ ] `SafetyRulesError` (`durable/publish.py:51`) is the only one of 38 names resolving to nothing.
- [ ] `xtb_minimum_refinement_attempts` has no `ge=` bound; at `-1` it raises `UnboundLocalError`.

## Checked and found sound

Recorded so the next reviewer does not re-derive them. **Auth is not stubbed** — the `entra_*`
settings that looked reader-less are read via the derived `entra_issuer_url` / `entra_jwks_endpoint`
(`api/auth.py:124,160`); RS256 is pinned, audience/issuer/exp checked, `kid`-less headers refused,
JWKS refetch rate-limited, 503 separated from 401. Also verified: no import breakage anywhere; the
token budget is booked on the disconnect path inside an `await`-free `finally`; audit coverage
survives the GxP removal intact and no code path issues UPDATE/DELETE on `audit_events`;
`message_from_row` really is the single deserializer; compaction is non-destructive; content is
suppressed on all 14 OpenInference `hide_*` paths by default and LangSmith egress is pinned off
(verified with the adversarial import ordering); no attacker-influenced metric label; the
admission/budget overshoot bound holds; the Helm chart correctly renders no Deployment for `chem`
and `safety`; and every tool name in `data/profiles/*.yaml` still resolves.

## Review

**What the instruction to measure bought.** Three claims changed under measurement rather than
argument. The `astream` tuple-arity coupling looked unpinned and turned out to be exercised by a
real compiled graph. The `entra_*` settings looked dead and are read through derived properties —
an auth "critical" that was a grep artefact. And the template-step fix I first wrote (invoke with
the whole call) was correct about the diagnosis and wrong as a remedy; only running the suite showed
it stringifying a `job` step's payload.

**What was harder than expected.** The grant fix's first draft planned to add `checkpoint_migrations`
to `CHECKPOINT_TABLES`. That constant is deliberately the conversation-bearing set and feeds
`DELETE … WHERE thread_id`, which `checkpoint_migrations` has no column for — the change would have
broken erasure and retention in order to fix a grant. The grant needed its own derivation.

**A failed approach, recorded so it is not retried.** Grouping the new grants per `setup()` and
guarding each group once. A `GRANT` on a missing table raises, and a raise anywhere in the `DO`
block aborts the whole reconciliation — so one interrupted `setup()` would have left *every* table
in the file ungranted, turning a narrow bug into a total one. Found by running it against a database
holding only `checkpoints`. Guarded per table instead.
