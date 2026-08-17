# BACKLOG

The things worth doing next, highest-consequence first. Top = next.

**This is a queue of what is still open, not a log of what was found.** A closed item is **deleted**
from it in the commit that closes it; the commit is the record and `git log` is the history. Do not
strike a row through, do not append "**Done**" under it, and do not add a dated section explaining
that a row above has gone stale. That is exactly how this file reached 4,717 lines and 237 open rows
in twenty-one days, growing about three lines for every line removed — the same failure `DEFERRED.md`
had and D-154 fixed there with this one rule.

**Rows are grouped by what they ask for, not by which review produced them.** A finding's date and
its reviewing pass are provenance, and provenance belongs in
[`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md) — the long-form record of every
row this queue has ever carried, including the ~185 that are open but not queued here. When a queued
row needs its full measurement history, that file has it under the review that found it.

**A row must name an anchor in the tree** — a module, a line, a manifest key — so any row can be
checked with one `grep` instead of an argument. A row that cannot name one is not ready to be
queued.

**A row is a claim about the code, and claims go stale.** A 2026-08-17 pass opened every anchor in
this file and found that roughly a third of the queue was not workable as written: four rows
described code that a merged decision had already deleted or fixed, eight were misstated in a way
that would have sent someone to the wrong function, and three carried their own deferral trigger and
belonged in `DEFERRED.md`. Two stated the opposite of what the tree does — one pointed at a
`DEFERRED.md` row that does not exist, and one said a data-subject erasure route was missing while
`make user-erase` implements it across nine tables with a dry run and per-table counts. **Before
working a row, check it against `HEAD`**; if it is wrong, the fix is to correct or delete the row,
and that is as much a contribution as the code would have been.

Related registers: [`DEFERRED.md`](DEFERRED.md) (postponed with the trigger that would revisit each),
[`docs/decisions/`](../decisions/) (why the system is the way it is; its README indexes the record by
topic).

---

## 1 — Untrusted input reaching a privileged surface

- [ ] **Four of the six endpoint-serving connectors are unauthenticated** — [M]. `bo`, `calc`,
      `molfp` and `rxnfp` ship `auth: mode: none`. The NetworkPolicy is the only thing between a pod
      in the namespace and a tool that starts durable work. The mode is not theoretical: `chem` and
      `safety` are served by `Chemclaw3-mcp`, whose `connector_app` enforces a bearer on `/mcp`
      itself, so their manifests set `mode: bearer` and `CHEMCLAW_CHEM_TOKEN` /
      `CHEMCLAW_SAFETY_TOKEN` are read per request today. The four remaining are the ones we host,
      and `connectors/server.py`'s `BearerAuthMiddleware` already enforces the mode from each
      bundle's own manifest, failing closed on an unresolved one.
      *Measured shape of the change:* four manifests, four `values.yaml` `optionalKeys`, the
      `tests/test_helm_chart.py` set that pins them, `.env.example`, and the dev/live runners —
      which need the vars or every call 401s. **Watch the name collision**: `CHEMCLAW_CALC_TOKEN` is
      already taken by a different hop (`core/config/calculators.py:166`, the token core presents to
      the *remote* calc server), so the calc connector's own `/mcp` needs a distinct variable.
      *Design direction:* MCP's OAuth 2.1 / ID-JAG token exchange for the federated case.

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — [M], and
      **narrower than this row used to claim**. It does not reach `job_records` or the audit trail:
      the durable path takes `actor` as an argument sourced from core's validated front-door
      principal (`durable/connector_job.py:302`), and never reads the header. The real reach is two
      columns on the synchronous MCP path — `bo_campaigns.opened_by` and `bo_suggestions.actor`, via
      `connectors/bo/server/tools.py:393`. The `unverified:<id>` marking is in place (D-2026-08-13),
      so what is open is that a caller still chooses the string. A bearer on the row above proves
      *core called*, not *which chemist*, so full closure needs an actor assertion bound to the call
      (OBO or a signed memo) — which is the `DEFERRED.md` warehouse row's blocker too.

- [ ] **No connector or MCP tool result is framed** — [M], wide half only. The two narrow channels
      are closed (`EvidenceChunk.source` is defanged, `recall_observations` frames its statements),
      and `agent/framing.py` is the pattern to reuse rather than reinvent — `frame_untrusted`,
      `defang`, `safe_id`, with a deployment-stable nonce. What remains is that **no connector
      result is framed at all**: `connectors/calc/server/tools.py`'s `fetch_artifact` hands
      arbitrary externally-produced text straight to the model, and none of the seven
      `wrap_tool_call` middlewares in `agent/langgraph_agent.py:594-646` is a framing one.
      This is ADR-sized rather than a patch: a middleware must not corrupt structured results
      (`ArtifactContent`, `EvidenceChunk`), so it needs a content-field convention first. The
      registry already answers "which tools are a connector's".

- [ ] **The stored-message conversion is a destructive in-place rewrite, run as a pre-upgrade
      hook** — [M]. `agent/message_migration.py:242` overwrites `session_messages.message` while its
      own docstring and `043_session_message_shape.sql:22` both promise the original stays readable.
      `migrate-job.yaml:10` runs it *before* any new pod exists, so it rewrites data the previous
      release is still serving with a reader that raises on the new shape — and `helm rollback`
      stays broken. Two separable halves: a preserved-original column (~15 lines), and moving the
      conversion out of the `pre-upgrade` Job into a `post-upgrade` one while the schema DDL stays
      where it is (~25 lines). Needs an ADR.

- [ ] **No live lane in this repo can start** — [M]. `infra/live/processes.sh:47` pins
      `CHEMCLAW_CONNECTORS_REQUIRED=true` while **chem and safety** are enabled and never started —
      measured, `build_composite()` serves `bo, calc, molfp, rxnfp` and `check_connectors_at_startup`
      raises. (This row used to name `calc` as a third; `calc` kept a local app after the physics
      move and *is* served.) `cli/connectors_dev.py:78` emits URLs only for bundles with a local
      app, so chem and safety keep their loopback defaults and the front door never boots. Also
      `infra/live/e2e-full-stack/up.sh:185` puts `$MCP_REPO/manifests` on `CHEMCLAW_CONNECTORS_DIR`,
      which `connectors/calc/connector.yaml:13` explicitly forbids — it survives only on
      `registry.py:124` being first-dir-wins, which **no test pins**.

- [ ] **The audit trail's `agent` column can never be non-empty** — [S]. `agent/audit.py:350` reads
      `get_current_specialist()`; `set_current_specialist` has **zero callers in `src/`** and
      `core/turn_signals.record_handoff` has none anywhere, tests included. `tests/test_audit.py`
      keeps the contextvar alive by setting it directly — the `map_to_hpc_identity` shape
      `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` names, which that ADR deleted
      three other controls for. **The answer is deletion, not wiring**: there is no specialist to
      name, and re-adding subagents is a new decision. Keep `HandoffEvent` (removing a union member
      is a coordinated three-repo change) and the SQL column (a merged migration is never edited);
      delete the contextvar trio, `record_handoff`, `HandoffSignal` and the audit write. ~120 lines
      out. That ADR simply did not sweep these.

## 2 — Answers that are wrong without saying so

- [ ] **A solvate collapses onto whichever fragment is larger** — [M], and worse than filed: it is
      not only the cache key, it is the **knowledge-graph note id**. Measured,
      `standard_smiles("CCN.C1CCOC1")` returns THF, and `compound_id("CCN.C1CCOC1")` equals
      `compound_id("C1CCOC1")` — a solvated compound and its solvent become one note.
      `core/chem.py:206` `FragmentParent` keeps the largest fragment and
      `_identity_survives_stripping` guards only organometallics and reactive metals.
      *Blast radius, measured rather than feared:* the D-011 calculation cache is **unaffected** (it
      keys on `require_canonical_smiles`), fingerprints have a designed invalidation lever
      (`STANDARDIZATION_VERSION` → `std5`), 0 of 68 shipped reagents change, and the committed
      corpus has 9 compound notes with **0 multi-fragment**. Candidate fix: keep every fragment when
      ≥2 are organic. Caveat that needs deciding, not assuming — that heuristic keeps the tartrate
      on nicotine bitartrate, which is arguably a salt; consulting the existing solvent table is the
      stricter variant.

- [ ] **On `openai_compatible`, one unsupported `response_format` degrades every judged answer for
      the life of the deployment** — [S]. Measured against a real loopback server: a server that
      rejects `response_format` with a 400, or ignores it and returns prose, lands in
      `agent/verifier.py:353`'s bare `except` and degrades to the citation gate on *every* call. The
      same contradicted-citation answer a working judge scores `confidence=0.0, unsupported=True`
      comes back `confidence=1.0, unsupported=False`. `score_answer` catches it (`verifier.py:448`
      forces `review_required` whenever `verified_by != "judge"`), and today it is the *only* caller
      of `verify_turn_answer` — so the danger is a future direct reader, not a live path. Fix is a
      pre-flight capability probe when `verifier_enabled` turns on, failing loudly at startup the
      way `_require_anthropic_key()` does, at the seam `api/app.py:155` already uses. Anthropic is
      unaffected. Both failure modes are already covered by loopback tests.

- [ ] **A retracted ELN entry stays current evidence** — [M]. A withdrawn entry that simply
      disappears from the export is invisible to a cursor-based sync, so the note it produced keeps
      answering as current. `RawEntry` has no tombstone and the `ElnAdapter` protocol's two methods
      cannot express one. The *amendment* half already works (`sync.py:275` re-proposes on a changed
      body); only disappearance is invisible. **The receiving end is already built** — `Note.valid_to`
      + `is_current(as_of)` — and `ingest/documents/sync.py:428 prune_share` is the same problem
      already solved for the share, including the three refusals that make a sweep safe ("an
      unreachable share and an empty one look identical"). Port that shape. Testable offline against
      a fake adapter; a real ELN is needed only to decide *which* mechanism the tenant offers.

- [ ] **Split-conformal uncertainty is unwired — and the function no longer exists** — [S].
      This row said `science/calc/uncertainty.conformal_uncertainty` "is correct and tested and has
      no caller". It has no *definition*: `uncertainty.py` records its deletion, and `Method` is
      `reported | propagated | none`. The row's framing is also wrong — it says wiring it must
      answer whether the interval attaches on the server, "which cannot see the ledger". It attaches
      here: `predict_solubility` (`connectors/calc/server/tools.py:660`) runs in this repo, holds
      the server's payload, and already calls `_log_prediction`; `reconciled_for` is one await away.
      So this is a re-add plus a call site (~100 lines), not a cross-repo decision. What it really
      needs is a policy answer: which predictors are calibrated enough to override their published
      RMSE. `calibration_conformal_coverage` / `_min_samples` come back with the caller.

## 3 — Work that is lost, dropped or invisible

- [ ] **A decided approval hold can be reopened** — [M]. `agent/interaction_tools.py::start_approval`
      passes no `id_reuse_policy`, so temporalio's default lets a decided hold be started again under
      the same id. `REJECT_DUPLICATE` is **not** the fix and the archive records why: expiry is not
      a decision — the workflow deliberately *completes* with `status="expired"` to release the run,
      and forbidding reuse would make that candidate unofferable forever while the button still
      renders. `ALLOW_DUPLICATE_FAILED_ONLY` fails identically, because an expired hold completes.
      The fix is to read the prior run's terminal outcome and refuse to restart only when it carries
      an actual decision. **Its stated blocker is gone**: the Temporal test server runs here
      (`tests/test_interaction_approval.py` is 3 passed, no skips), so both paths are exercisable.

- [ ] **A pinned template's arguments go unchecked once its bundle stops being ours** — [S].
      `cli/validate_templates.py` reads signatures from `connectors/<name>/server/tools.py`, so a
      bundle we declare but do not run has none — `hazard-briefing` calls `screen_hazards` and is
      name-checked only. The loss is *reported* (`unchecked_arguments`), which is what makes this a
      row rather than a defect. **This row's proposed fix location is wrong**: `make
      connector-validate` also has no live session — it imports the bundle's local module and
      returns `[]` for exactly these bundles — and it runs inside `ci`, which must stay offline. The
      check belongs on the **live lane**, opening real sessions via `open_connector_specs` where
      `BaseTool.args_schema` carries the names, as a new target beside `live-probes`.
      `make template-validate` stays offline and keeps the note.

- [ ] **A timed-out attachment parse still runs to completion** — [L], not [M].
      `agent/attachments.py:284` shields the future deliberately, so the timeout bounds the caller
      and the slot, never the thread — a hostile document holds a worker forever. The only real fix
      is a killable subprocess, with pickling and a new child-OOM failure mode to classify
      (~150-250 lines). **The cheap, honest half is separable**: `ingest/documents/sync.py:204` calls
      `asyncio.to_thread` with no `wait_for` at all, so one pathological file can hold the sync
      activity indefinitely; giving it the bound the front door already has is ~10 lines.

- [ ] **A surface cannot tell a waiting plan from a stalled one** — [M], **restated**. This row used
      to say the LangGraph rebuild "did not carry" an `awaiting-job:` marker. It was deleted on
      purpose, twice (`D-2026-08-11`, re-confirmed `D-2026-08-12`), and `agent/state.py:16-29` is
      the docstring saying so. It cannot be cleanly restored either: `Todo` is upstream's TypedDict
      written by a model-facing tool, and prefixing `content` would perturb `plan_identity`'s hash
      so an approved plan revokes its own approval the moment it starts a job. What is genuinely
      missing is any **link from a job to a plan step** — the surface gets `JobStartedEvent` and
      `job_records` but nothing joins them to a todo. That is a design task.

## 4 — Operating it

- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart
      and no statement of who runs either. `docs/guides/runbook.md:925` states what this system
      *requires* of those stores and documents a Postgres restore procedure — what does not exist
      anywhere is tooling that performs or **verifies** a restore, and that cannot be built against
      a store this repo does not own. (The former separate "no backup tooling" row is folded in
      here; it was downstream of this one and overcounted the stores.)

- [ ] **The image vulnerability scan is not merged as a gate** — [M]. The runbook's false claim that
      it runs is corrected (2026-08-17) and `tests/test_deploy_chart.py` now fails if the runbook
      names a gate nothing runs. The gate itself is still absent: `trivy` appears nowhere in
      `.github/workflows/image.yml`, which already builds `chemclaw:ci` locally on every PR, so the
      step needs no registry. Held for a stated reason — per D-2026-08-01 the candidate scan
      reported `setuptools` 70.3.0 and `msgpack` 1.1.2 while an exhaustive `find / -xdev` in the
      same build listed neither, and a gate whose last word contradicts the artifact it scanned
      makes every red build ambiguous. Re-check that against a current trivy before merging.

- [ ] **The background worker is a hard singleton** — [M]. `workers.background.replicas: 1` owns ELN
      sync, memory synthesis, retention and eval drift, and cannot be scaled because the PR-gate
      checkout lock is host-local (`kg/git_submitter.py:101`, `fcntl.flock`, D-069). This row used
      to say the distributed lock "is its own `DEFERRED.md` row" — **there is no such row**, there
      never was, and the cross-reference defeated the rule that a row must name a real anchor. The
      lock is buildable here: a Postgres advisory lock on the pool that already exists, ~60 lines.

- [ ] **Egress is still port-scoped by default** — [S]. `networkPolicy.egressDestinations` is
      declarable and empty, which renders `to: []` — any destination on the allowed ports, as the
      template's own comment says. The chart cannot invent a site's CIDR, so the sound fix is to
      make empty **fail** when the policy is enabled, with an explicit `allowAnyDestination: true`
      escape hatch. ~15 lines plus tests, fully offline (the chart tests parse YAML).

- [ ] **Three credentials are plain `str` on the settings object** — [S], **corrected**. The hazard
      this row stated is already closed: `core/logging.py:449` redacts all nine secrets by exact
      value across message, args, `exc_text` and `stack_info`, and its docstring names a settings
      `repr` as a covered route — so `logger.debug("%s", settings)` is safe today, and as of
      2026-08-17 so is logging's own `handleError` path. What is left is defence in depth on
      `llm_api_key`, `hpc_api_token` and `temporal_api_key` — **4 read sites**, ~20 lines. The three
      DSNs are explicitly *not* in scope: 43 sites all feeding psycopg conninfo, which needs the
      plain string straight back. Rotation is a separate concern with no anchor and is dropped.

- [ ] **No session pagination and no per-session delete** — [M], **corrected**. This row claimed a
      data-subject erasure request "has no route across the seven tables". It does:
      `agent/leaver.py` erases across **nine** tables in one transaction with per-table rowcounts
      and a dry-run default, shipped as `make user-erase`. What is actually missing is (a) cursor
      pagination — `session_store.list_for_owner` truncates at `service_max_listed_sessions` with no
      cursor, so older sessions are unreachable, and (b) `DELETE /sessions/{id}`, which `leaver`
      does not offer because it is actor-scoped, not session-scoped.

---

## Everything else

~185 further open findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md),
grouped by the review that found them, with their full measurements. They are open, not abandoned —
promote one into the queue above when it becomes the next thing worth doing, and delete it from
here when it is done.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Entra tenant, real
Temporal broker, real cluster, real HPC, real Snowflake — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.
