# Gap-closure plan — implementing `docs/audit/12-capability-gap-analysis.md`

> Companion to `docs/implementation-tickets.md` (F0–F9) and `docs/parity-plan.md` (F10). Same
> conventions: config-not-magic-numbers (`CHEMCLAW_` prefix, one `pydantic-settings` source,
> `extra="forbid"`), docstrings state the *why*, `mypy --strict`, durability stays in Temporal
> (D-002), agent-authored knowledge goes through the PR-gate (D-005), and **no abstraction without a
> second real caller** (Rule of Three). A step is done when its tests pass **and** `make lint type
> test` is green.

Phase label: **F11**. ADRs are appended to `DECISIONS.md` as D-083…D-085, one per wave.

---

## Ordering principle

The analysis sequenced by *value*; this plan sequences by *dependency*, which reorders two things:

- **Config + schema first.** Several waves add config keys and schema fields that later waves read
  (`TOOL-2`'s resolver feeds `KNW-4`'s vocabulary and `TOOL-3`'s hazard screen; `KNW-1`/`KNW-2`'s
  fields feed the retrieval and memory layers). Landing them early keeps later diffs small.
- **Reachability before the capabilities it exposes.** `RCH-1`/`RCH-2` are the machine-consumed
  call sites that `AGT-6` (structured outputs) has been waiting for, so they precede it.

---

## W0 — Deployment truth (`DEP-1`, `DEP-2`, `DEP-3`, `SCH-2`, `RCH-3`)

**Goal:** a chart that can actually run the knowledge layer, and an approval hold with a human on
the other end.

- **W0.1 `DEP-3`** — flip `mcp.<name>.enabled` to `false` in `values.yaml` and make the template's
  guard match its own stated intent (a networked transport gates the pods, not the bare flag).
  *Touch:* `deploy/helm/chemclaw/values.yaml`, `templates/deployment-mcp.yaml`.
- **W0.2 `DEP-1`** — knowledge volume + sync. A shared `emptyDir` mounted into the service and both
  workers, populated by a git-sync **init container** (first fill, blocking) plus a periodic pull so
  merged notes reach live pods. `CHEMCLAW_KNOWLEDGE_DIR` becomes a chart value. The read path needs
  no change — `kg/graph.py`'s `(path, mtime_ns, size)` fingerprint already busts the cache on sync.
- **W0.3 `DEP-2`** — a fourth secret (`knowledgeRepoToken`) + `CHEMCLAW_NOTE_REPO_DIR` value + a
  writable clone for the background worker (the only component that submits notes).
- **W0.4 `SCH-2`** — `NoteReindexWorkflow` on `background-jobs` wrapping the existing
  `reindex_notes`, registered on the worker and added to `planned_schedules()` +
  `OWNED_SCHEDULE_IDS`. Config `note_reindex_schedule_minutes`.
- **W0.5 `RCH-3`** — `GET /approvals` + `POST /approvals/{id}/decision` (authenticated,
  owner-scoped), delivering the existing Temporal `decide` signal; two buttons in `app.js`.
  Deliberately **not** an agent tool — the agent must not approve itself.

**Done when:** the chart renders with a knowledge volume and a push credential, the reindex fires on
a Schedule, and an opened hold can be decided by a human through the front door.

## W1 — Reachability (`RCH-1`, `RCH-2`, `RCH-5`, `RCH-4`, `AGT-1`)

**Goal:** every built subsystem is invocable, and an abandoned turn stops costing.

- **W1.1 `RCH-1`** — `agents/report_tools.py::request_development_report(topic, sections)`; the QM
  seam verbatim (`require_actor` → `authorize_trigger` → deterministic workflow id → return id).
- **W1.2 `RCH-2`** — `agents/campaign_tools.py::start_optimization_campaign` +
  `get_campaign_status`. Fixes the dangling pointer in `skills/experiment-design/SKILL.md`.
- **W1.3 `RCH-5`** — emit `PlanEvent` from harness todo state and `JobStartedEvent` when a tool
  starts a durable job (closes the F2 deferral; `plan_only` is the chart default, so the plan must
  be renderable).
- **W1.4 `RCH-4`** — `propose_note` emits a session event carrying the branch ref, so the chemist
  sees their proposal land; `GET /proposals` lists open `note/*` branches.
- **W1.5 `AGT-1`** — turn cancellation: the SSE generator handles `CancelledError`, releases the
  admission permit, and records the partial turn against the budget.

**Done when:** report + campaign are startable from chat, the plan is visible, proposals are
traceable from the session, and closing the tab stops the work.

## W2 — Chemistry the prompt already promises (`KNW-1`, `KNW-2`, `TOOL-2`…`TOOL-5`)

- **W2.1 `KNW-2`** — `purity_percent` + `impurities: list[Impurity]` on the outcome; adapters map
  them when present; the mass-balance validator ignores them (they are analytics, not stoichiometry).
- **W2.2 `KNW-1`** — `performed_at: date | None` on `OrdReaction`; the ELN→note mapper populates
  `Note.valid_from` from it, finally feeding F10-G2's bi-temporal fields.
- **W2.3 `TOOL-2`** — `chemclaw/identity_resolution.py` + `resolve_compound(name)` tool: a committed
  synonym table for the common bench reagents first, then the fingerprint store's known SMILES,
  then an optional external resolver behind the F7 source seam (off by default, no network in CI).
- **W2.4 `TOOL-3`** — `chemclaw/hazard.py`: deterministic GHS/H-code lookup over resolved
  components, a binary incompatibility matrix, and a SMARTS set for energetic/peroxide-forming
  motifs. Exposed as `screen_hazards`, wired into the protocol-design skill as a **must-call**, and
  registered as a `hazard` metric in `evals/` so the behavior is pinned. LLM judgment stays in the
  skill; the screen itself is deterministic.
- **W2.5 `TOOL-4`** — `stoichiometry_table(...)` over the existing mass-balance chemistry.
- **W2.6 `TOOL-5`** — `render_structure(smiles)` → SVG via RDKit (already a dependency); the UI
  inlines it.

## W3 — Operate it (`SCH-1`, `SCH-3`…`SCH-5`, `DEP-4`, `AGT-2`)

- **W3.1 `SCH-1`** — `workflows/retention.py`: per-table retention from config. `audit_events` is
  hash-chained, so its policy is **archive-then-reseal** (export the pruned prefix, record a genesis
  anchor row) — never a bare `DELETE` that would read as tampering.
- **W3.2 `SCH-3`** — `ScheduleOverlapPolicy.SKIP` + per-job jitter on every planned Schedule.
- **W3.3 `SCH-4`** — schedule health recorded per run and surfaced by `DEP-4`'s metrics.
- **W3.4 `SCH-5`** — `AuditChainVerifyWorkflow` on the drift cadence, alerting via the must-deliver
  notify seam.
- **W3.5 `DEP-4`** — `GET /metrics` (Prometheus text format, no new dependency): turn
  rate/latency/error, shed turns, budget refusals, audit-sink failures, schedule health.
- **W3.6 `AGT-2`** — mid-turn durable-job resume: the runner awaits the existing push-back mailbox
  within one streamed turn, bounded by config.

## W4 — Depth and ideation (the remainder)

`KNW-3` outcome_class + failure-mode notes · `KNW-4` conditions vocabulary · `KNW-5` graph analytics
+ gap queries · `KNW-6` note-type registry · `KNW-7` compound notes · `TOOL-1` networked MCP
transport · `TOOL-6` literature retriever · `TOOL-7` units at the LLM boundary · `AGT-3` file
ingress · `AGT-4` per-user preferences · `AGT-5` clarifying questions · `AGT-6` structured outputs ·
`SCH-6` inbound events · `IDEA-1` standing queries · `IDEA-2` predicted-vs-actual calibration ·
`IDEA-3` PMI/E-factor as a BO objective · `IDEA-4` dry-run mode · `IDEA-5` source-tier weighting ·
`IDEA-6` corpus backfill · `IDEA-7` prose↔code contract test.

`IDEA-7` is pulled forward into W1 on its own merits: it is S-effort, it would have caught two of
the findings being fixed in W1/W2, and it is the deterministic half of the AG-13 deferral.

---

## Cross-cutting rules for every wave

1. **Default-off where a path changes.** New retrieval/verification/tool paths ship inert so the
   classic behavior stays load-bearing.
2. **One config source.** Every threshold/URL/interval is a `CHEMCLAW_`-prefixed field in
   `chemclaw/config.py`, mirrored into `.env.example` and (where deployment-relevant) `values.yaml`.
3. **Tests prove behavior, not mocks.** Postgres/Temporal tests skip offline exactly as the existing
   ones do; the deterministic core of every new capability is tested in-memory.
4. **No new store, no new orchestrator.** Everything lands on Postgres + Temporal + the Git graph.
