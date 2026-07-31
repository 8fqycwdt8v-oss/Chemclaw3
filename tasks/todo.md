# Task: close the gaps found by the dataflow review — persistence, reachability, visibility, knowledge tiers

Requested 2026-07-31. Branch: `claude/chemclaw-dataflows-architecture-gi467d`.
ADRs: **D-158** (written), **D-159**, **D-160**, **D-161** (reserved in `docs/decisions/README.md`).
Renumbered from D-154–D-159 on merge: another branch landed its own D-154/D-155 first, and the
branch merging second renumbers (`CLAUDE.md`).

The review is in the session's two artifacts (a dataflow atlas and a gaps/proposals companion).
This file is the implementation plan for every proposal it made. **All of W1 is shipped except
W1.4, which is held pending a decision, and W2.1 with it. W2.2 onward and all of W3 are still plan
only.**

(The previous occupant of this file, the restructure-consistency pass, is merged; its record is
D-156. The one before it, the agentic-system review, is D-145 and D-151…D-153.)

## The four findings this plan closes

1. **The expensive calculation is the one that isn't cached.** `calc` writes every result to
   `calculation_results`; `qm` touches no store at all. A 2-second xTB energy is cached forever
   (D-011, never evicted); a multi-hour DFT run on the cluster is cached nowhere. Its only durable
   homes are Temporal history and a note that exists *only if a human merges the PR*. After Temporal
   retention rolls, the byte-identical request re-runs the whole cluster job.
2. **The calculation store is write-once, lookup-only.** `ResultStore` is `get(key)`/`put(stored)`
   and nothing else. Every calculation ever run is in Postgres, and the agent cannot ask "what have
   we already computed for this scaffold?". `kg/crosslink.py` is a finished read side with **no
   producer** — nothing in `src/` populates `calc_refs`.
3. **The work is invisible while it happens.** Token streaming is genuinely incremental, but a tool
   call is announced only *after* it returns, tool results never reach a surface as data, and no SSE
   `ping` is configured — so a working 20 s calculation and a hung server look identical.
4. **Knowledge has exactly one tier and one gate.** There is no proactive cross-project learning
   loop; `record_confirmed_answer` fires only on explicit confirmation, per chat, and is PR-gated.

## Correction to the review, found while planning

The review said the UI never opens `GET /sessions/{id}/events`. **That is true only of the bundled
dev page** (`src/chemclaw/api/static/app.js`). The real frontend opens it properly —
`src/hooks/useJobFeed.ts` in `Chemclaw3_ui`, with abort-on-unmount, exponential backoff with jitter,
and a dedicated `429` path. The actual defect is one step later: `job_completed` lands in
`ChatState.jobFeed`, which **has zero readers**. Backend push-back works end to end and dies in the
last mile. Two further UI findings, both new:

- `capability_degraded` and `tool_failed` are absent from the UI's type contract (`shared/events.ts`
  says "Ten members"; the backend emits twelve). `normalizeEvent` fails them against its allowlist
  and returns `null`, so they are dropped **silently** — a partial answer renders as a normal one.
  The allowlist is deliberate forward-compat, which is why the backend may add events first.
- `GET/POST /sessions/{id}/plan[/decision]` are **not proxied** by the BFF (`server/routes.ts`), so
  harness plan approval is unreachable from the browser; `Prompts.tsx` still uses a stale
  send-a-chat-message fallback for it.

---

## W1 — Make the work visible (ADR **D-159**)

The turn-event contract is shared across two repos, so it gets one ADR covering both sides. The
UI's unknown-type allowlist makes this safely ordered: **backend first, UI second**, with no
breaking window.

### Backend (`src/chemclaw/api/`)

- [x] **W1.1 Announce a tool call when it is issued, not when it returns.** — shipped, D-159 (completeness-by-parse, not a separate start event). Today `_ToolCallTrace`
      flushes when an update passes without adding to the call — and for a streamed call that
      terminating update is the one carrying the *result*. Split the lifecycle: emit `ToolCallEvent`
      at issue, and let the existing `tool_failed` / a new `tool_result` close it. This is the
      single change that converts the worst dead-air window (20 s inline calc waits, 60–120 s MCP
      timeouts) into visible progress.
      *Acceptance*: a test with a fake agent that streams a call then a delayed result asserts the
      `tool_call` event is yielded **before** the result update is consumed.
- [x] **W1.2 Add `ToolResultEvent`** — shipped, D-159. Success-only: a raised call already has `tool_failed`, so no `ok` field. to `api/events.py` (`tool`, truncated `preview`, `ok: bool`),
      emitted on completion. Reuse the existing `_ARG_PREVIEW_CHARS` truncation discipline. Right
      now a computed number reaches the chemist only as the model's paraphrase, and a turn that dies
      after a successful calculation loses it entirely.
      *Acceptance*: the value appears in the stream independently of the model's own text.
- [x] **W1.3 Configure SSE `ping`** — shipped, D-159 (`service_sse_ping_seconds`, default 15). on both `EventSourceResponse` constructions (the turn stream and
      the job-event stream). Neither passes `ping=` today, so there is not even a transport-level
      keepalive during a long tool wait.
- [ ] **W1.4 Open the stream before admission.** — HELD, still needs the requester's call: it turns a 503 under load into a 200 whose body reports the problem, which is an API contract change for any client retrying on 503. The durable turn claim and the semaphore both
      complete before `EventSourceResponse` is constructed, so a queued turn waits up to
      `service_turn_admission_timeout_seconds` (5 s) with no response and may then 503. Move
      acquisition inside the generator and emit a `queued` event first.
      *Note*: keep the 409/429 pre-checks where they are — those are genuine refusals, not queueing.
- [x] **W1.5 Bundled dev page** (`api/static/app.js`) — shipped. All three cases added and the
      push-back stream opened with the session. The drift itself was the finding: three events
      reached the union over three changes and none reached the page, because a missing `case`
      falls through to `default` and looks exactly like an event that was never sent. So the fix
      is `tests/test_dev_page_events.py` — the union checked against the page's switch labels in
      both directions — not just the three cases.

### Frontend (`Chemclaw3_ui` — separate branch, separate PR)

- [x] **W1.6 Restore the test toolchain first.** — done as part of W1.7 (vitest + happy-dom restored, `.tsx` added to the include pattern; the stale `ISSUES.md`, the missing `check-openapi.mjs` and the absent playwright config are still open). `vitest` and `happy-dom` are absent from
      `package.json`/lockfile while `vitest.config.ts` and 751 lines of suites still reference them,
      so `npm test` cannot run; `scripts/check-openapi.mjs` and any `playwright.config.*` do not
      exist. Nothing else in this workstream is verifiable until this is fixed. Also refresh the
      stale `ISSUES.md` claims (issues 2 and 3 are closed).
- [x] **W1.7 Render the job feed.** — shipped (Chemclaw3_ui `claude/render-job-feed`). `jobFeed` is written by `useJobFeed` and read by nothing. Surface
      completed durable jobs in the conversation (a card in `TracePanel`, or a toast). This switches
      on an entire finished backend subsystem.
- [x] **W1.8 Add `capability_degraded` and `tool_failed`** — shipped (Chemclaw3_ui #6), and
      `tool_result` followed in Chemclaw3_ui #7 once W1.2 landed. That one also lifted
      `TracePanel`'s "invocations only" caveat, which was the honesty constraint the missing event
      had forced. A result completes the row of the call it answers rather than adding a second
      row; a `tool_failed` closes its call's row too, or a failed call would read "running…" for
      the rest of the conversation.
- [x] **W1.9 Proxy and use the plan-approval routes.** — shipped (Chemclaw3_ui #8). Both routes
      whitelisted; `Prompts.tsx`'s "Approved — go ahead." chat message replaced with the real
      hash-bound call. The plan is read when the card appears, not when a button is pressed, so
      the hash posted back is the hash of the plan the human read; a 409 re-reads and asks again
      rather than retrying with the new hash. 409 also needed its own error kind — it means "a
      turn is already running" on the message route and "the plan changed" on this one.

---

## W2 — Make computation durable and reachable (ADR **D-158**)

One decision: *a durable connector result is persisted in the calculation store like any other
computation, and that store is readable.* W2.1 and W2.2 are the two halves.

- [x] **W2.1 Persist the QM result.** — shipped, D-158. Constraints found while planning, all load-bearing:
      - The workflow is deterministic and cannot touch Postgres → persistence is a **new activity**
        on `bundle_queue("qm")`, called after `parse_qm_output`.
      - `connectors/qm/specs.py` is a **strict leaf module** (`tests/test_connector_isolation.py`
        forbids imports beyond pydantic + `core.{config,chem,ids}`), so `CalculationKey` must **not**
        be imported there. Build the key in `activities.py` or a new sibling module.
      - `qm_job_key` is a bare 16-hex digest and is **not** a `calc_refs`-valid string (it fails
        `_CALC_REF`). Build a real `CalculationKey(calc_type="dft", calc_version=…, inputs=…)`.
        Follow the `calc` convention: molecule in `inputs`, method/basis/pipeline version in
        `calc_version` (mirroring `XtbSpec.calc_version()`), not everything folded into the input
        hash as `qm_job_key` does.
      - `calc_type` needs no registration — it is a free string. `"dft"` passes `_CALC_REF`.
      - `note_from_qm_result` takes only `QMJobResult` today; thread the key in so the note can
        carry `calc_refs`. This makes it the **first producer** for the already-built
        `kg/crosslink.py` read side.
      - Add the new activity to `tests/temporal_env.py::QM_ACTIVITIES` or the workflow test hangs.
      - Config flag on `HpcSettings` (`qm_persist_to_calc_store: bool = True`), documented in
        `.env.example` (parity is enforced, DA-1).
      *Acceptance*: an end-to-end `QMJobWorkflow` test asserts a `calculation_results` row exists
      after the run and that the returned note's `calc_refs` resolves to it; a second identical run
      is served from the store.
- [x] **W2.2 Give the store a query surface, then a tool.** — shipped, D-163. Migration **024**,
      not 023: D-157's `job_records` took that number while this was still plan. The plan's
      "filter by SMILES" turned out not to be uniform — the xTB task family and the geometry
      pointer key on a 3-D structure, which a molecule does not determine — so that combination is
      *refused* rather than answered with an empty list, which would read as "nothing has been
      computed". Value-range filtering was dropped deliberately: the payload is calculator-owned
      and the store has been calculator-agnostic since D-011.
      - `ResultStore` is `@runtime_checkable`, so a new query method must be added to **both**
        `PostgresStore` and `InMemoryStore`.
      - `input_hash` is `stable_hash(canonical_smiles)` and non-reversible — query *by molecule* by
        hashing the query molecule, not by scanning. Migration **023** adds an `input_hash` index
        (`001`'s index is `(calc_type, calc_version)` only). Never edit an applied migration.
      - Expose as `find_calculations` — an MCP tool on the `calc` connector (`connector.yaml`
        `endpoint.tools:` + a `@server.tool()` in `connectors/calc/server/tools.py`). `find_` is not
        in `_MUTATING_PREFIXES`, so `connector-validate` accepts it. Filters: SMILES, calc type,
        method/version, date range, value range.
- [ ] **W2.3 `fetch_artifact`** on the same connector, over `PostgresArtifactStore.open`/`list_for`.
      Hessians and optimized geometries are stored, eviction-managed, and unreachable; a note can
      cite an `artifact_ref` the agent cannot open. Note `_MEDIA_TYPES` already reserves
      `density.restart` and `orbitals.molden` — "nothing writes these yet" becomes false with W2.1.
- [ ] **W2.4 Generalize `calculator_trust`.** It reduces the `predictions` ledger to six aggregates
      for two hardcoded property names. Make it accept any registered calculator and add an
      outlier/residual listing scoped by substructure or tag, so the agent can say "this predictor
      is unreliable for *this kind* of molecule" — which is what trust means in practice.
- [ ] **W2.5 Metadata-filtered similarity.** `similar_reactions` returns `(id, label, similarity)`,
      so "similar reactions that used Pd and gave >80% yield" is unanswerable. Let
      `FingerprintReactionRetriever` accept the same `type`/`tag` filters the note retrievers take
      and resolve through `note_index`, so the filter applies **before** truncation.

---

## W3 — Knowledge tiers (ADRs **D-160**, **D-161**)

### W3.1 Provenance-aware retrieval (D-160) — prerequisite, do first

- [ ] `EvidenceChunk` carries `content`, `source_note_id`, `retriever`, `score`, `conflicts_with` —
      and **no provenance**. `created_by`, `confidence` and `source` are not in what the model sees,
      even though `NoteRef` already exposes all three. The graph read path does not distinguish
      `created_by` anywhere; `confidence` is a ranking signal for truncation order, never a filter.
      Today that is harmless because everything readable was human-merged. **It becomes a
      correctness bug the moment a second tier exists**, so it ships first and on its own.
      - Add provenance fields to `EvidenceChunk`; populate from the note in `_chunks_from_hits`.
      - **Layering constraint**: `tests/test_layering.py` forbids `chemclaw.retrieval` from importing
        `chemclaw.agent` — including `agent.framing`. Framing stays at the `gather_evidence` call
        site; only the data moves.
      - Teach the answer contract to qualify a claim resting on a low-confidence or agent-authored
        note.

### W3.2 Cap the memory jobs (rides D-161)

- [ ] The three synthesis jobs run daily, rescan the whole corpus with no cursor, and have **no cap**
      on notes produced (worst case ≈1.5 notes per corpus reaction per day, plus one retirement note
      per superseded cluster, each its own branch and PR). In practice it stays quiet because of
      three good accidents — ids anchor on the cluster's smallest member so a grown cluster reuses
      its branch, a byte-identical note produces no diff and no push, and force-push-with-lease
      updates in place. But nothing *bounds* it, and a large corpus import would produce a PR flood
      on the first night. Add `max_notes_per_run` with the dropped count logged explicitly — the
      repo's own "no silent caps" rule, applied to the one place with no ceiling.

### W3.3 The observations tier (D-161)

The proposal in one line: **the human gate does not disappear, it moves from every observation to
the few worth promoting.**

- [ ] **Store in Postgres, not git** (migration **024**). Git's value is human review, diff and
      audit; with no review it buys PR noise and repo churn and returns nothing. A table gives cheap
      upsert-counters for accumulating support, TTL eviction (the `ArtifactEvictionWorkflow` is the
      precedent), and no branch-per-note explosion. This *preserves* "git is the source of truth"
      precisely because observations are explicitly **not** truth. Follow the
      `agent/subscriptions.py` idiom (module SQL constants, `db.connection`, explicit commit).
- [ ] **Shape**: `statement`, `scope` (transformation class / chemotype / step), `evidence_note_ids`,
      `projects_seen`, `support_count`, `contradiction_count`, `first_seen`, `last_seen`,
      `status ∈ {open, promoted, retired}`, `origin ∈ {interaction, corpus-mining}`.
- [ ] **Generation**: a scheduled Temporal workflow on the core `background` queue, shaped exactly
      like the existing memory jobs (`durable/memory_jobs.py`) — no new infrastructure, the same
      constraint D-019 imposed on the memory layers. Mines (a) merged-corpus clusters below the
      playbook bar and (b) **interaction history across sessions**, which is the thing that does not
      exist today. Register in `cli/schedules.py` (`OWNED_SCHEDULE_IDS` + `planned_schedules`),
      behind `observations_enabled: bool = False`.
- [ ] **Retrieval discipline (load-bearing)**: observations return in a **separate, labelled bucket**
      — never fused into the same ranked list as notes. One added instruction, parallel to the
      episodic-vs-semantic separation the architecture already demands: *an observation may direct
      what you look for; it may never be the evidence for a claim.* An answer resting only on an
      observation must say so.
- [ ] **Anti-feedback rule — the dangerous failure mode.** Support counts **distinct merged evidence
      notes** only; observation-cites-observation is structurally forbidden (a schema constraint, not
      a guideline). Otherwise the agent writes an observation, later retrieves its own observation,
      counts it as corroboration, and inflates past the promotion threshold into a PR — a
      self-confirming loop wearing the costume of cross-project evidence.
- [ ] **Promotion**: crossing ≥N distinct projects and ≥M distinct merged evidence notes with no open
      contradictions auto-opens **one** PR proposing a `playbook` note, through the existing
      `propose_note`. No second write path (D-019/D-078's rule).
- [ ] **Decay + instrumentation**: unsupported observations retire on a window; track the promotion
      rate. If nothing ever promotes, the tier is a write-only log and should be deleted rather than
      defended.
- [ ] **Contradiction detection comes nearly free**: `kg/conflicts.py` already flags same-compound,
      concurrently-valid notes with a confidence gap; "this observation contradicts merged knowledge"
      is exactly the signal a process chemist wants.

---

## Sequencing

Independent, parallelizable: **W1 backend**, **W2**, **W3.1**, **W3.2**.
Ordered: **W3.1 → W3.3** (hard prerequisite). **W1.6 → W1.7/W1.8/W1.9** (UI, tests first).
**W1.2 → W1.8's `tool_result` half** (cross-repo; backend first, UI tolerates unknown types).

Recommended first two: **W1.7** (dozens of lines of frontend switch on a finished backend subsystem)
and **W2.1** (stops paying twice for cluster time and closes STO-7's remaining direction).

The fix is two-part and worth copying: derive the literal from the setting's own default so it
cannot drift again, *and* assert the directory exists, because an empty corpus and a wrong path
produce identical numbers.

Per workstream: the acceptance check named above, plus `make lint type test` green. A connector or
tool change also needs `make connector-validate` and `make prose-validate`; a note-schema change
needs `make kg-validate`; a new schedule needs `tests/test_schedules.py` to still pass with the id
added to `OWNED_SCHEDULE_IDS`. Every new test must be **verified to fail on the unfixed code** by
reverting the source — the standard this repo already holds itself to.

Frontend: `npm run typecheck` plus `npm test`, which requires W1.6 first.

## Open questions for the requester

1. **W3.3 thresholds** — N projects and M evidence notes. I would start at N=2, M=3 (the playbook
   job already uses ≥2 projects) and tune against the promotion rate, but it is a domain call.
2. **W2.4 scope** — is generalizing `calculator_trust` worth it before more calculators are
   calibrated? Only `solubility` and `pka` are wired today.
3. **W1.4** — moving admission inside the generator changes what a client sees under load (a stream
   that says `queued` rather than a 503). Confirm that is the wanted behaviour.
