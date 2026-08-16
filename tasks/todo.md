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

## Fixed (each proven by reproduction)

- [x] **CRITICAL — the grant reconciliation stripped write access to every LangGraph table on the
      second deploy.** `REVOKE ALL ON ALL TABLES` reaches the six tables `setup()` creates; the
      enumerated re-grants named none. Reproduced in Postgres 16: the app role *owns* `checkpoints`
      and still ends with `INSERT=f SELECT=t`. The guard was blind — `_tables()` knew only
      `infra/sql/*.sql`, so `_DYNAMIC`'s explicit `checkpoints` entry was discarded. ADR
      `D-2026-08-16-a-revoke-reaches-tables-the-grants-never-name`.
- [x] **HIGH — a dry run could write a durable memory.** Fixed with a path-aware predicate rather
      than by adding names: one verb serves `/memories/` and `/scratch/`.
- [x] **HIGH — a template step read a tool's refusal as its answer**, and the audit trail booked the
      refused call as `ok`. Measured: on the plain-args form this path used, a failed MCP tool
      returns a bare `str`. Invoking with the whole call was tried and rejected — it stringifies a
      `job` step's dict payload.
- [x] **HIGH — every failed tool call emitted both `tool_failed` and `tool_result`, and the error
      text joined the corpus `score_answer` grades grounding against.** `graph_stream` read a
      `status` that `answered_failure` rewrites to `"success"`; `ToolFailureSignal` now carries
      `call_id` and the stream suppresses on the turn's own failure set.
- [x] **HIGH — a subagent's tool calls, results and plan were emitted as the main agent's.** Work
      below the root is now marked, and its plan withheld rather than relabelled — `PlanEvent` has
      no `agent` field, so there is nowhere to say whose it is.
- [x] **HIGH — subject erasure missed the full text of every tool result.** `tool_result_blobs` is
      now erased by the session its links name. The links themselves are left to the cascade, which
      is what lets the grant keep withholding DELETE on that table — a conflict the grants test
      caught.
- [x] **HIGH — the remote calc client had no session timeout**, so the only live bound was httpx's
      un-overridden 300 s rather than the 900 s its setting names, and it fires *silently*. Both
      bounds are now set, session-first.
- [x] **HIGH — `CALCULATION_EPOCH` reached no calc cache key.** Folded into `remote_key`'s params
      hash; the three documents that prescribed bumping it now describe something that works.
- [x] **MEDIUM — a Postgres failure was reported as a calculation-server outage.** The blanket
      `except` spanned the `yield`, so the caller's `cached_compute` body re-entered it. Narrowed,
      with `_call` converting its own transport failures.
- [x] **MEDIUM — a reaction result stamped a locally-configured `method`** describing a calculation
      this process did not run. `SpeciesEnergy` now carries the server's.
- [x] **MEDIUM — an empty-answer turn also yielded an empty answer** and booked itself
      `completed=True` in the cost ledger.
- [x] **MEDIUM — the reference UI spliced a subagent's prose into the answer bubble.**
- [x] **MEDIUM — `calc_session` had no test at all**, which is why the three defects in it were
      invisible to a green suite.
- [x] **MEDIUM — the image still installed xtb and crest** (~200 MB, and a GPL-3.0 redistribution
      decision) for two modules that left with the physics; **and nothing in `deploy/` pointed
      anything at the calculation server**, so `helm install` produced a `calc` bundle failing
      against loopback.
- [x] Record drift: `ARCHITECTURE.md`'s specialist team, `science/__init__.py` calling `calc` "the
      physics", the dead `SafetyRulesError` entry, the unbounded
      `xtb_minimum_refinement_attempts`, the present-tense handoff docstrings, `CLAUDE.md`'s three
      non-existent backlog rows, and the one closed `[x]` row.

Suite after: **3991 passed**, `ruff` and `mypy --strict` clean, seven of eight validators green
(`helm-validate` needs the `helm` binary, absent here; the chart change is covered by
`tests/test_helm_chart.py` and `tests/test_deploy_chart.py`).

## Open

Queued as `docs/planning/BACKLOG.md` rows, each naming an anchor. The largest are:

- **~25 calculator settings with no reader**, seven of which look like live pKa/solubility
  calibration but are baked into the *server's* `calc_version`.
- **`tblite` is a runtime dependency with no importer**, kept alive by the test that derives
  `ALPB_SOLVENTS` — a launch gate for four durable jobs — from a local install rather than from the
  server that now decides it.
- **`list_artifacts`/`fetch_artifact` read a store with no writer**, alongside a live eviction
  schedule, eight settings and a migration.
- **The stored-message conversion is a destructive in-place rewrite run as a `pre-upgrade` hook**,
  against data the previous release is still serving. Needs an ADR, not a patch.
- **`xtb_geometry_decimals` still shapes half of every remote cache key.**
- **No live lane in this repo can start**, and the e2e harness does what `calc`'s manifest forbids.
- **A retrieval leg that raised is indistinguishable from one that found nothing.**
- **The audit trail's `agent` column can never be non-empty**, and `memory_store()` repeats the
  cold-start race `checkpointer.py` was fixed for.
- **Retention's checkpoint `LIMIT` bounds the deletes, not the scan.**
- **`message_from_row` degrades on one branch and mislabels the speaker on the other.**

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
