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
row this queue has ever carried. 223 of its rows are open
(`grep -c '^- \[ \]' docs/archive/findings-2026-08.md`; the header here said "~185" until
2026-08-17). That is not "223 *further*" and the two counts do not subtract: promoting a row
**restates** it, so a queued row is still open there under its original wording, and matching the
two sets by title matched only 7 of the 30 rows this queue held when that was measured (it holds 41
now — §5 added eleven, and none of those eleven is in the archive at all, because that section is
the first thing here that is not a defect this repository found in itself). The overlap is real and
unmeasurable by `grep`, which is why the number here is the archive's own and not a difference. When
a queued row needs its full measurement history, that file has it under the review that found it.

**A row must name an anchor in the tree** — a module, a line, a manifest key — so any row can be
checked with one `grep` instead of an argument. A row that cannot name one is not ready to be
queued.

**A row is a claim about the code, and claims go stale.** A 2026-08-17 pass opened every anchor this
file then held and found seventeen rows not workable as written: four described code that a merged
decision had already deleted or fixed, eight were misstated in a way that would have sent someone to
the wrong function, and three carried their own deferral trigger and belonged in `DEFERRED.md`. Two
stated the opposite of what the tree does — one pointed at a `DEFERRED.md` row that does not exist,
and one said a data-subject erasure route was missing while `make user-erase` implements it across
**twelve** tables with a dry run and per-table counts
(`python -c "from chemclaw.agent.leaver import _ERASE; print(len(_ERASE))"` → 12: seven always,
plus the checkpointer's three and the store's two, each skipped when the deployment has not created
it). **Before working a row, check it against `HEAD`**; if it is wrong, the fix is to correct or
delete the row, and that is as much a contribution as the code would have been.

Ten further rows arrived from concurrent reviews while that pass ran and are carried here unedited —
they postdate it and have not been re-verified against `HEAD` by anyone but their author, which is
exactly the state the paragraph above is about.

**And the pass that wrote the two paragraphs above is not exempt from them.** An audit later the
same day found its own numbers stale in the way it was written to catch: it said "nine tables" for
an erasure that clears twelve; it filed a hazard as unpinned that
`tests/test_connector_registry.py:293` had already pinned; and five of the anchors it wrote no
longer resolved to the construct they named by the time the branch was audited, four of them off by
a handful of lines. None of that is neglect — it is a measurement taken hours before the tree moved
under it, which is the failure mode this file has rather than an exception to it. Re-measure on the
way past; a row you had to correct before you could work it is a row that was worth opening.

Related registers: [`DEFERRED.md`](DEFERRED.md) (postponed with the trigger that would revisit each),
[`docs/decisions/`](../decisions/) (why the system is the way it is; its README indexes the record by
topic).

---

## 1 — Untrusted input reaching a privileged surface

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — [M], and
      **narrower than this row used to claim**. It does not reach `job_records` or the audit trail:
      the durable path takes the actor as an argument sourced from core's validated front-door
      principal (`ConnectorJobInput.requested_by`, `durable/connector_job.py:126` — the row named a
      field called `actor`, which does not exist), and never reads the header. The real reach is two
      columns on the synchronous MCP path — `bo_campaigns.opened_by` and `bo_suggestions.actor`, via
      `connectors/bo/server/tools.py:393`. The `unverified:<id>` marking is in place (D-2026-08-13),
      so what is open is that a caller still chooses the string. A bearer on the row above proves
      *core called*, not *which chemist*, so full closure needs an actor assertion bound to the call
      (OBO or a signed memo) — which is the `DEFERRED.md` warehouse row's blocker too.

- [ ] **`fetch_artifact` is a tool that can only refuse** — [S]. `list_artifacts` and
      `fetch_artifact` promise "the relaxed coordinates, the second derivatives, the raw vibrational
      spectrum" and "a conformer search seeded from a known structure", and two agent profiles
      (`computation`, `evidence`) instruct the agent to reach for them before proposing a rerun. In
      this repository the **only** artifact writer left is `ArrayOffloadingStore`, which writes
      `hessian.npy` and `dipole_derivatives.npy` — and `fetch_artifact` refuses binary by design.
      Measured: `list_artifacts` returns one `application/x-npy` entry and `fetch_artifact` on it
      raises "is binary … not text". `xtbopt.xyz`, `crest_conformers.xyz` and `vibspectrum` appear
      only in `science/calc/artifacts._MEDIA_TYPES` and in comments; their writers left with the
      engines in `D-2026-08-16-the-physics-leaves-the-cache-stays`. **The geometry half is now
      answered elsewhere** — D-2026-08-21's `structures` store is the addressable geometry the
      profiles were really asking for — so what is left is the *spectrum*: either the server returns
      `vibspectrum` as an artifact, or these two tools stop advertising one. Note the row below
      assumes `fetch_artifact` hands the model externally-produced text; today it cannot hand it
      anything.

- [ ] **No connector or MCP tool result is framed** — [M], wide half only. The two narrow channels
      are closed (`EvidenceChunk.source` is defanged, `recall_observations` frames its statements),
      and `agent/framing.py` is the pattern to reuse rather than reinvent — `frame_untrusted`,
      `defang`, `safe_id`, with a deployment-stable nonce. What remains is that **no connector
      result is framed at all**: `connectors/calc/server/tools.py`'s `fetch_artifact` hands
      arbitrary externally-produced text straight to the model, and none of the seven
      `wrap_tool_call` middlewares in `agent/langgraph_agent.py:594-631` is a framing one.
      This is ADR-sized rather than a patch: a middleware must not corrupt structured results
      (`ArtifactContent`, `EvidenceChunk`), so it needs a content-field convention first. The
      registry already answers "which tools are a connector's".

- [ ] **The stored-message conversion is a destructive in-place rewrite, run as a pre-upgrade
      hook** — [M]. `agent/message_migration.py:242` overwrites `session_messages.message` while its
      own docstring and `043_session_message_shape.sql:20` both promise the original stays readable.
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
      which `connectors/calc/connector.yaml:13` explicitly forbids — it survives on
      `connectors/registry.py:124` (`found.setdefault`) being first-dir-wins, and **that behaviour
      is pinned**: `tests/test_connector_registry.py:293` builds two dirs holding a bundle both
      named `alpha`, on ports 7777 and 8888, and asserts the *first* dir's endpoint is the one
      `enabled()` returns. (This row said "no test pins" it until 2026-08-17, which invented a
      second hazard on top of a real one — the ordering is a load-bearing dependency of the live
      lane whether or not it is pinned, and it is.)

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

- [ ] **`CalculationKey`'s primary key is an unescaped concatenation of caller-shaped strings** —
      [M]. `science/calc/store.py:122` (`CalculationKey.as_str`) builds the literal
      `calculation_results` primary key as `f"{calc_type}@{calc_version}:{input_hash}:{params_hash}"`
      (`infra/sql/001_calculation_results.sql:7`), and `calc_version` is not guaranteed free of `@`
      or `:` — `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` gives real
      examples (`esol-delaney@2004`, `cal-0.28733:-29.3116`). Two different `(calc_type,
      calc_version)` pairs can serialise to the identical string (`calc_type="a", calc_version="b@c"`
      vs. `calc_type="a@b", calc_version="c"`); if the hash pair also matched, one calculator's
      `ON CONFLICT (key) DO UPDATE` (`science/calc/postgres_store.py:32`) would silently overwrite
      another calculator's cached row with a different `result`. The fix — deriving the key from
      `stable_hash` over the four components as a mapping, the way `molecule_hash`/`input_hash`
      already do — changes every existing row's key, which under D-011 ("never recomputed") is a
      full-cache invalidation on deploy; that trade needs an ADR and a migration plan, not a quiet
      change to `as_str`.

## 2 — Answers that are wrong without saying so

- [ ] **A solvate collapses onto whichever fragment is larger** — [M], and worse than filed: it is
      not only the cache key, it is the **knowledge-graph note id**. Measured,
      `standard_smiles("CCN.C1CCOC1")` returns THF, and `compound_id("CCN.C1CCOC1")` equals
      `compound_id("C1CCOC1")` — a solvated compound and its solvent become one note.
      `core/chem.py:206` `FragmentParent` keeps the largest fragment and
      `_identity_survives_stripping` guards only organometallics and reactive metals.
      *Blast radius, measured rather than feared:* the D-011 calculation cache is **unaffected** (it
      keys on `require_canonical_smiles`), fingerprints have a designed invalidation lever
      (`STANDARDIZATION_VERSION` → `std5`), **0 of the 68 shipped reagents change**, and the
      committed corpus has 9 compound notes with **0 multi-fragment**. The 68 is
      `core/reagents.py::_RAW_SYNONYMS` — 97 spellings over 68 distinct SMILES, 21 of them
      multi-fragment — named here because the row quoted the number without its corpus and an
      auditor looking for it found `data/vendored/records.csv` (35 rows) instead and read the row
      as invented.
      *What the same measurement turned up, and it strengthens the row:* three of those 68 —
      LDA, HATU and TBTU — **already** collapse today (`CC(C)[N-]C(C)C.[Li+]` →
      `CC(C)NC(C)C`, i.e. LDA recorded as diisopropylamine), and `data/vendored/records.csv`'s
      sodium tert-butoxide already becomes tert-butanol. Those are D-2026-08-01's counterion rule
      working as designed, not this defect — but they are why the candidate fix is stated as it is.
      Candidate fix: keep every fragment when ≥2 are organic. Measured, that changes **none** of the
      68 and leaves all three salt collapses in place, which is the point: it is a solvate rule, not
      a re-litigation of the salt one. Caveat that needs deciding, not assuming — the heuristic
      keeps the tartrate on nicotine bitartrate, which is arguably a salt; consulting the existing
      solvent table (`science/calc/solvents.py:44`) is the stricter variant.

- [ ] **On `openai_compatible`, one unsupported `response_format` degrades every judged answer for
      the life of the deployment** — [S]. Measured against a real loopback server: a server that
      rejects `response_format` with a 400, or ignores it and returns prose, lands in
      `agent/verifier.py:372`'s bare `except` and degrades to the citation gate on *every* call. The
      same contradicted-citation answer a working judge scores `confidence=0.0, unsupported=True`
      comes back `confidence=1.0, unsupported=False`. `score_answer` catches it
      (`agent/verifier.py:467` forces `review_required` whenever `verified_by != "judge"`), and
      today it is the *only* caller of `verify_turn_answer` — so the danger is a future direct
      reader, not a live path. Fix is a pre-flight capability probe when `verifier_enabled` turns
      on, failing loudly at startup the way `_require_anthropic_key()`
      (`agent/llm_provider.py:305`) does, at the seam `api/app.py:166`
      (`check_connectors_at_startup`, inside `_lifespan`) already uses. Anthropic is unaffected.
      Both failure modes are already covered by loopback tests.

- [ ] **A retracted ELN entry stays current evidence** — [M]. A withdrawn entry that simply
      disappears from the export is invisible to a cursor-based sync, so the note it produced keeps
      answering as current. `RawEntry` has no tombstone and the `ElnAdapter` protocol's two methods
      cannot express one. The *amendment* half already works (`ingest/eln/sync.py:201` — a body that
      is not byte-identical to the merged note falls through and is re-proposed); only disappearance
      is invisible. **The receiving end is already built** — `Note.valid_to`
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

- [ ] **The digest is written to a mailbox with no reader, and the watermark advances anyway** —
      [L]. `durable/digest.py:146-166` writes to `session_events` under session id `digest-<owner>`
      with `kind="digest"`, and the only consumer in the tree is `GET /sessions/{id}/events`, which
      claims `kinds=("job_completed","job_failed")` and sits behind `resolve_session` — so it 404s
      that id and would filter the kind out anyway. Measured: the route's exact claim against a real
      `digest` row returned `[]` and left it unconsumed. `notify_session_best_effort` returns `True`
      on a successful *insert*, so `acknowledge_digest` fires and `mark_reported` moves the
      watermark past notes the subscriber will never see; `_is_new` can never re-qualify them, and
      `durable/retention.py:133` (`_PRUNABLE`'s `consumed_at IS NOT NULL` predicate) makes the
      orphaned rows immortal. The same
      dead end exists for `system-eval-drift`, whose must-deliver stance therefore guarantees
      delivery to nobody. Needs a route (`GET /digests` claiming `kinds=("digest",)`) — and until
      one exists, `digest_enabled` should plan no Schedule, since shipping the ack without the
      reader loses matches permanently rather than merely not delivering them.

- [ ] **A rejoined durable run never reaches the second chemist** — [M].
      `connectors/jobs.py:386-403`: on `WorkflowAlreadyStartedError` the launcher returns the id and
      deliberately emits no `record_job_started`, and the running workflow's `session_id` belongs to
      the *first* launcher. So chemist B gets no turn-stream `job_started`, no `job_completed`, and
      `agent/job_results.py` cannot wait on it either — they are told "in progress" and must poll by
      hand forever. The comment justifies the silence with "it may already be finished";
      `handle.describe()` answers exactly that question, so the ~3-line fix is to describe once on
      the rejoin path and announce it when the status is RUNNING. Full push-back to a second session
      is the larger change behind it.

- [ ] **The sixteen periodic workflows can still hang instead of failing** — [M]. The job path now
      declares `failure_exception_types` and `tests/test_workflow_registry.py` holds it
      (`D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs`), scoped deliberately: for a run
      nobody is waiting on, parking a redeploy bug until someone ships a fix is a defensible trade
      and the opposite of the one taken there. Decide it per workflow rather than by widening the
      test — retention and the memory jobs are the ones worth arguing about, since a parked run
      there is invisible in exactly the way the fan-out drop was.

- [ ] **`connector_job_timeout_seconds` bounds a 20-second job and a 24-hour job identically** —
      [M]. `core/config/connectors.py:71`: one global 90,000 s ceiling is the child's
      `execution_timeout` for every bundle, so if the `calc` worker is down a 20 s xTB job sits
      `running` for a day with no signal, while the setting is sized entirely by the QM path. An
      optional `JobSpec.timeout_seconds` applied as `min(declared, setting)` would let a bundle
      lower its own ceiling while the deployment keeps the maximum, leaving
      `_the_job_ceiling_covers_the_poll_it_bounds` untouched.

## 4 — Operating it

- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart
      and no statement of who runs either. `docs/guides/runbook.md:925` states what this system
      *requires* of those stores and documents a Postgres restore procedure — what does not exist
      anywhere is tooling that performs or **verifies** a restore, and that cannot be built against
      a store this repo does not own. (The former separate "no backup tooling" row is folded in
      here; it was downstream of this one and overcounted the stores.)

- [ ] **The image vulnerability scan is not merged as a gate** — [M]. The runbook's false claim that
      it runs is corrected (2026-08-17) and
      `tests/test_deploy_chart.py::test_every_supply_chain_gate_the_runbook_names_actually_runs`
      keeps it corrected. **State the guarantee, not the implementation:** no supply-chain tool the
      runbook's §(xiv) claims — in the gate table *or* in the prose beside it — may be one that
      `image.yml` does not actually execute. It does not prove the named gate is *blocking*, only
      that something runs it. This row said it "fails if the runbook names a gate nothing runs",
      which was one degree stronger than the assertion then in the tree: the check was a substring
      over the workflow, so a comment naming the tool satisfied it, and only backticked table rows
      were read at all. Both holes were found and closed the same day — which is the argument for
      naming the guarantee rather than the mechanism, since the mechanism changed under this row
      within hours of it being written. The gate itself is still absent: `trivy`
      appears nowhere in
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
      this row stated is already closed: `core/logging.py:972` (`SecretRedactingFilter._redact`)
      redacts all nine `_SECRET_SETTINGS` values by exact match across `msg`, `args`, `exc_text` and
      `stack_info`, and the module docstring (`core/logging.py:23`) names "a `repr` of a config
      object" as a covered route — so `logger.debug("%s", settings)` is safe today, and as of
      2026-08-17 so is logging's own `handleError` path. What is left is defence in depth on
      `llm_api_key`, `hpc_api_token` and `temporal_api_key` — **5 read sites in 4 modules**
      (`agent/llm_provider.py:251`, `core/embeddings.py:232`, `connectors/qm/hpc/nextflow.py:53`,
      `core/temporal_client.py:74` and `:75`), ~20 lines. The three DSNs are explicitly *not* in scope:
      **34 lines** read one (`grep -rno "settings\.\(postgres_dsn\|postgres_migration_dsn\|session_store_dsn\)" src/ --include=*.py | sed 's/:settings.*//' | sort -u | wc -l`
      — 41 occurrences over 27 modules; the row said 43 and never said what it was counting), all
      feeding psycopg conninfo, which needs the plain string straight back. Rotation is a separate
      concern with no anchor and is dropped.

- [ ] **No session pagination and no per-session delete** — [M], **corrected**. This row claimed a
      data-subject erasure request "has no route across the seven tables". It does:
      `agent/leaver.py:161` (`_ERASE`) erases across **twelve** tables in one transaction with
      per-table rowcounts and a dry-run default, shipped as `make user-erase` — seven unconditional,
      plus the checkpointer's three (`CHECKPOINT_TABLES`) and the store's two (`_MEMORY_ERASE`),
      each skipped when a deployment has not created it. (The row said nine until 2026-08-17 — a
      hand count of a tuple whose length is seven literals plus two splats, which is why it is now
      `len(_ERASE)` and not as a reading of the source.) What is actually missing is (a) cursor
      pagination — `session_store.list_for_owner` truncates at `service_max_listed_sessions` with no
      cursor, so older sessions are unreachable, and (b) `DELETE /sessions/{id}`, which `leaver`
      does not offer because it is actor-scoped, not session-scoped.
- [ ] **`session_owners` and `session_turns` grow without any age-based disposal** — [S].
      `infra/sql/README.md`'s own `session_owners` row already flags this ("survives its session's
      pruned history; BACKLOG") but no row existed here to match it — this closes that dangling
      cross-reference. Neither table is in `durable/retention.py`'s `_PRUNABLE` set, and the only
      `DELETE` against either is `agent/leaver.py`'s manual, actor-scoped erasure — so every session a
      client ever created (the companion UI creates one on the first keystroke, before any message is
      sent) leaves a `session_owners` row forever, even after `session_messages` for that session is
      fully pruned by age. Needs a policy decision — prune once a session has no remaining
      `session_messages` and is past the retention window, or explicitly accept unbounded growth and
      say so — not a code change made unilaterally.

- [ ] **`observations_status_idx` does not cover the query it was built for** — [S].
      `infra/sql/025_observations.sql:50` indexes `(status, last_seen DESC)`, with a comment saying
      "the retrieval bucket wants open observations newest-first" — but `memory/observations.py:122`
      (`_SELECT_OPEN`) actually sorts `ORDER BY cardinality(evidence_note_ids) DESC, last_seen DESC`,
      an expression the index does not cover. The index serves the `status='open'` filter only; every
      read still sorts all open rows in memory by an unindexed expression. Whether the fix is an
      expression index matching the real sort or a correction to which one is authoritative is a
      product call — the migration's stated rationale and the code that ships disagree about what the
      "newest and most-evidenced first" bucket actually orders by.

- [ ] **`connectors.<name>.enabled` in the chart never reaches the agent** — [M].
      `values.yaml:135` says "CHEMCLAW_CONNECTORS_ENABLED in `config` below decides which bundles
      the agent loads at all" — and that key is in none of the 33 `config` entries. The chart derives
      `CHEMCLAW_CONNECTOR_URLS`, `SERVICE_FLEET_REPLICAS` and `PG_FLEET_POOLED_PROCESSES` from
      `.Values.connectors` and not the enable list, so `enabled: false` removes the pods and leaves
      the tool on the agent's surface: the launcher starts the wrapper on the polled queue and its
      child on `connector-qm`, which nobody polls, and the chemist is told "running" until the 25 h
      ceiling. Latent today (all seven shipped entries are `enabled: true`); it fires the first time
      someone uses the switch the file documents. Fix is a `chemclaw.connectorsEnabled` helper
      mirroring `connectorUrls`, plus deleting the sentence that points at the absent key.

- [ ] **A jobs-only bundle has no reachability signal at all** — [M]. `connectors/health.py:81-99`
      derives its target from `health_url(manifest)`, which is `None` for a bundle with no
      `endpoint:` — so `qm` reports `unprobed` whether its worker fleet is at two replicas or zero,
      `chemclaw_connectors_unhealthy` counts only `unreachable`, and `check_connectors_at_startup`
      raises only on `unreachable`. The fail-fast posture an operator opts into is structurally
      blind to the failure with the largest blast radius. `describe_task_queue(bundle_queue(name))`
      in the same sweep, reported as `unpolled` and counted like `unreachable`, is the runtime twin
      of the manifest check `connector-validate` now does — and it catches the row above too.

- [ ] **One `replicas` knob drives two differently-shaped Deployments** — [S].
      `templates/deployment-connectors.yaml:35` and `:98` both read `$cfg.replicas`, so scaling
      `calc`'s MCP server to 4 also scales its Temporal worker to 4, and `pooledProcesses` counts it
      twice against the `pg_fleet_max_connections` startup ceiling. Worse, the guard requires
      `replicas` only when there is no `url`, while the worker block is deliberately not conditioned
      on `url` — so a `url:` bundle that owns durable work renders an empty `replicas` (Kubernetes
      defaults to 1) and contributes `nil | int` = 0 to the declared fleet. Split into
      `serverReplicas`/`workerReplicas` defaulting to `replicas`, and extend the chart test to
      require it whenever `worker` is set.


## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **~14.7k tokens of context are spent before the user says anything, and 8.6k of it is
      deferrable** — [M]. Measured on this checkout: `_INSTRUCTIONS` 13,187 chars (~3.3k tokens), the
      29 in-process tool schemas from `agent/chemclaw_agent.py::_capability_tools` 34,303 chars
      (~8.6k), the skills listing `SkillsMiddleware` publishes 11,156 chars (~2.8k) — at the
      `chars / 4` estimator `agent_context_token_budget` itself uses. Progressive disclosure works on
      skills (38.6k tokens of bodies held back for 2.8k spent) and does nothing on tools: every
      schema is sent on every turn, and the surface is headed for 19 servers (`Chemclaw3-mcp/MODULES.md`
      catalogues 19, 5 built). Anthropic's `defer_loading` + tool search keeps definitions
      discoverable without spending the context; programmatic tool calling measured −38% billed input
      tokens on a 75-tool agent with no accuracy change. **Do the cheap half first**: a test that
      asserts the floor, so the next tool is not free at review time and paid for on every turn
      forever. Then put the cold tail behind search — success is the floor under ~8k with no probe
      losing its `expects_tools` name.

- [ ] **17 of 67 agent-callable tools are named by no probe, and nothing would notice the next one**
      — [M]. Measured over `data/evals/probes/*.yaml` against
      `agent/chemclaw_agent.py::available_tool_names()`: 232 probes, 50 tools named, 17 never —
      `compute_interaction_energy`, `run_conformer_refinement`, `render_structure`, `read_attachment`,
      `remember_preference`, `recall_preferences`, `forget_preference`, `list_watches`,
      `stop_watching`, `task`, `write_todos`, and all six filesystem verbs. The hole is not random: it
      is the *newest* surface — the scratchpad and memory tools the M-phases added, and the subagent
      seam three ADRs argue about. `tests/test_repo_map.py` already proves the pattern for keeping a
      declaration honest in both directions; the same shape here (fail when an agent-callable tool is
      named by no probe) closes it permanently. Then write the 17 probes, scratchpad and memory first.

- [ ] **Half the probe corpus tests one tool** — [S]. `gather_evidence` is in `expects_tools` for 116
      of 232 probes; `find_notes` 91; `expand_note` 60; the tail is thin. Two consequences worth
      separating: the corpus mostly measures one retrieval path, and ChemToolAgent's finding — that
      tool augmentation **does not consistently beat the base LLM**, and hurts on general chemistry
      questions — cannot be reproduced here. Bucket C (51 probes) scores restraint but never runs the
      same question tool-free for comparison. `evals/ab.py::compare_tool_utility` is already written
      and already registered as `plan_execute_utility`; an A/B arm over bucket A is mostly wiring.

- [ ] **No external benchmark has ever been run** — [M]. `make eval` gates 23 metric values over 14
      case files, a **7-document** retrieval corpus and a **39-note** knowledge graph, with the
      science half resting on one solubility value, one BO regret replay and two mass balances. It is
      honest and it is not comparable to anything. ChemRAG-Bench (1,932 expert-curated chemistry QA
      pairs) is the best first target because it scores the retrieval half — where this system's
      science actually lives — and it runs against an OpenAI-compatible endpoint, which is exactly the
      seam `agent/llm_provider.py` already has. ChemBench and AstaBench are the follow-ups. A number
      somebody else can also produce is the only kind that survives an argument with a chemist.

- [ ] **Two durability layers are maintained where upstream now ships one** — [L]. Temporal's
      LangGraph plugin reached public preview on 2026-07-16 (Temporal Python SDK ≥1.27, Python ≥3.11):
      the graph runs as a workflow, each node as an activity, execution is checkpointed at every node,
      and `interrupt()` pauses **durably** and resumes on a signal. This repository hand-built the
      equivalent — `agent/checkpointer.py` on its own autocommit pool, `agent/plan_approval_store.py`,
      the job→session push-back in `durable/` — and carries two open defects in exactly that seam (*"A
      decided approval hold can be reopened"*, *"A rejoined durable run never reaches the second
      chemist"*) plus the durable-approval-store row this file already holds. **This row asks for an
      evaluation, not a migration**: does `interrupt()`-as-signal close those three together, and what
      does it cost in the D-2026-08-10 §3 line that layer 1's checkpointer holds turn state and
      nothing else. An ADR either way.

- [ ] **The agent cannot execute code, and the decision on record is narrower than the consequence** —
      [L]. `agent/scratchpad.py::scratchpad_tools` withholds `execute` for a correct reason: deepagents
      0.7 ships exactly one concrete sandbox (`LangSmithSandbox`, declined on content-egress grounds)
      and `LocalShellBackend` is documented as unrestricted. That is a refusal of *two specific
      sandboxes*. What is not on record is a decision about whether an execution substrate belongs at
      all — and the consequence of not having one is that anything not pre-wrapped as a tool is
      unreachable: no ad-hoc unit conversion, no fit, no re-parse of an unexpected output file. Every
      comparable chemistry agent executes (ChemCrow, Coscientist, El Agente, OpenClaw). The number to
      test the decision against: **El Agente Gráfico** replaced multi-agent chat with a typed execution
      runtime where large objects stay native and measured 168.4 → 9.1 LLM requests (−94.6%), 1.65M →
      284k tokens (−82.8%), 4.5× wall-clock, *and* rubric score 88.25% → 90.94%. An ADR either way.

- [ ] **`deep-research` has no index behind it** — [M]. `agent/research_tools.py::gather_evidence`
      sweeps the knowledge graph, the ELN, the mounted document share and the fingerprint store —
      every one internal. `skills/deep-research/SKILL.md` describes a capability whose corpus is
      whatever notes exist (39 on this checkout). `Chemclaw3-mcp/MODULES.md` files `litsearch`
      (Europe PMC / OpenAlex / Crossref bulk, built at image time, no egress) as *proposed*, and says
      in as many words that it "gives Chemclaw3's existing `deep-research` skill a real index".
      ChemRAG measured **+17.4% average relative gain** from a chemistry corpus and — the design input
      that matters — that corpus choice is task-dependent: reaction prediction wants literature,
      nomenclature wants structured databases. A process chemist asking "has anyone run this coupling
      on a deactivated aryl chloride" currently gets whatever those 39 notes happen to say.

- [ ] **Compaction drops what a narrower argument would let it keep** — [M].
      `agent/compaction.py::disabled_summarizer` turns upstream's summarizer off for a reason that is
      right: a summary is new model prose over content `agent/framing.py` marked untrusted, and the
      envelope does not survive it. So the conversation window **deletes** older groups instead. The
      objection is to *summarisation*, and it does not reach the lossless half: replacing a
      re-fetchable tool result with a placeholder (Anthropic's `clear_tool_uses_20250919`) invents no
      prose and so cannot launder an envelope — and in Anthropic's own research-agent measurement the
      lossless half did most of the work (peak 335k → 173k). LangChain's `context_editing` middleware
      is the in-stack form. Keep the window as the floor; add clearing above it.

- [ ] **Cost is metered but never evaluated** — [S]. `agent/turn_cost.py` and `core/metrics.py` record
      what a turn spends; no case in `data/evals/cases/` scores it. HAL (21,730 rollouts, 9 models, 9
      benchmarks) found the most expensive model on the accuracy/cost Pareto frontier in **1 of 9**
      benchmarks, and that higher reasoning effort *reduced* accuracy in a majority of runs — neither
      is visible to a suite that scores only accuracy. `evals/metric.py`'s `@metric` registry takes a
      float; the meter already produces one. This is the cheapest row here and it is what makes
      `model_routes` a decision instead of a preference.

- [ ] **The `Chemclaw3-mcp` catalogue is not re-derived against releases** — [S]. `MODULES.md` files
      `retro` as *adopted, not built*, `admet` against "ADMET-AI / DeepChem", and `nomenclature`
      (OPSIN, MIT, local) as "the best value-to-effort ratio in the catalogue". Since those
      assumptions were written: **Boltz-2** shipped MIT-licensed, approaching FEP accuracy on binding
      affinity at ~1000× the efficiency; **ether0** shipped Apache-2.0 as a 24B open-weights chemistry
      reasoning model that beats frontier general models on open-answer chemistry and molecular
      design; RetroReasoner/Retro-R1 changed what a retrosynthesis server would wrap. A catalogue is a
      claim about what is available, and claims go stale — the same rule this file states about its
      own rows. Re-derive those three entries; the OPSIN one is probably still the right first build.

- [ ] **Nothing watches for upstream shipping a decision we made ourselves** — [S], and it is the
      meta-row the four above are instances of. `make upstream-check` guards the six *shapes* this
      repo borrows against a dependency bump — the coupling that breaks. Nothing guards its
      *decisions* against upstream shipping the thing: the Temporal LangGraph plugin row above is five
      weeks old and reached no list here. `tests/test_upstream_surface.py` is the right precedent
      (two of its assertions are *absences*, so upstream fixing something turns the workaround red).
      The cheap version is a dated section in this file, re-derived when a dependency is bumped,
      listing what each pinned upstream now ships that this repository implements itself.

---

## Everything else

223 open findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md)
(`grep -c '^- \[ \]'` on that file), grouped by the review that found them, with their full
measurements. That set **overlaps** this queue rather than extending it — promotion restates a row,
so a queued row is still open there under its original wording, and the header's "~185 further"
was a subtraction nobody could reproduce. They are open, not abandoned — promote one into the queue
above when it becomes the next thing worth doing, and delete it from here when it is done.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Temporal broker, real
cluster, real HPC, real Snowflake — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.

## Backfill the ORD corpus on the four-repo lane's first bring-up

`infra/live/e2e-full-stack/up.sh` seeds `CHEMCLAW_DATA_SOURCES=graph,eln-json,eln-ord` and points
`CHEMCLAW_ORD_EXPORT_DIR` at `Chemclaw3_mock`'s 10,011 ORD exports, and then never syncs them from
an early enough cursor. All 10,011 share one mtime — the moment the repo was cloned — and carry
older payload timestamps, so once the sync cursor passes that instant none of them can ever
qualify again. Chemclaw3 handles this correctly and loudly (`adapter.py::warn_late_arrivals`, one
aggregated WARNING naming the remedy); the gap is that the harness never takes the remedy. A first
bring-up should run `ElnSyncWorkflow` with an explicit early `since` before anything advances the
cursor, so the ORD half of the mock's data is actually reachable in an end-to-end pass. Found by
the 2026-08-17 full-stack run — see `tasks/live-test/full-stack-e2e-2026-08-17.md`.

## Surface `invalid_tool_calls` — an unparseable tool call is currently a silent no-op

LangChain puts a tool call whose arguments do not parse into `AIMessage.invalid_tool_calls` rather
than `tool_calls`. Nothing in `src/` reads that field, so the agent — which iterates `tool_calls` —
drops the call without a `tool_failed`, a `tool_result`, or any other trace. Proven outside the
stack via `langchain_openai.chat_models.base._convert_dict_to_message`: truncated arguments yield
`tool_calls: []` and a populated `invalid_tool_calls` carrying the parse error.

A truncated argument document is what a real model emits when a stream is cut or a token budget
runs out, so this is reachable in production, not only under the storm's mock. With no prose the
turn ends as `empty_answer`; **with prose it proceeds as though no tool were needed**, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` exactly.

Fix in the middleware chain: after the model call, convert each `invalid_tool_calls` entry into a
visible failure the model can also correct from. Do not jump from `after_model` — see
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`. Verify with
`make live-storm`'s `f-malformed-json` check, which currently fails. Found by the 2026-08-17
full-stack run — see `tasks/live-test/full-stack-e2e-2026-08-17.md`.
