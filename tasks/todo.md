# Backlog features — assessment + implementation plan

Source: `BACKLOG.md` (every open item) + the open rows of `DEFERRED.md` it points at.
Plan with the per-item assessment: **`docs/backlog-plan.md`**.
Branch: `claude/backlog-feature-assessment-dytr10`.

## Assessment (done)

- [x] Enumerate every open backlog item (31) and check each claim against the tree, not memory.
- [x] Assess each against the five questions (trigger held? real defect? offline-verifiable?
      KISS/Rule of Three? cost vs value) → BUILD / DEFER / DROP / BLOCKED.
- [x] Write `docs/backlog-plan.md`: verdict table, specs for the survivors, triggers for the rest.
- [x] Correct the backlog entries whose claims are false today (the DROP verdicts).

Result: **8 BUILD · 14 DEFER · 5 DROP · 12 BLOCKED**. No BUILD item needs live infra.

## Stale entries corrected in `BACKLOG.md`

- [x] F4-T5 "per-request role → skills scoping" — already delivered by D-052
      (`agents/skill_access.py::RoleScopedSkillsSource`, wired at `agents/chemclaw_agent.py:139`).
- [x] 1b.5 Temporal lookup/persist activities — folded into 1c.5 by design; checkbox never cleared.
- [x] `QMJobWorkflow`→`CalculationWorkflow` rename — dropped, not deferred: the workflow type name
      is durable-history state, so the rename is the exact un-versioned change C1's policy forbids.
- [x] OKF per-bundle `log.md` — dropped *as designed* (concurrent note branches all append to one
      file = manufactured merge conflicts); redesign as a generated view recorded with its trigger.
- [x] Design caution "apply skills/tools selectively, measured per task" — satisfied by `evals/ab.py`
      (2b.4) + `AgentProfile` (D-075); nothing left to build.
- [x] F0-T4 stand-in-server variant — dropped (would test the stand-in; the client-wiring half is
      already proven by `tests/test_harness_execution.py`, D-058). Live-endpoint half stays blocked.

## Build queue (specs in `docs/backlog-plan.md` §3)

### Wave A — silent failures made visible [S]
- [x] **A1** ELN late-file detection — aggregated WARNING when a file arrives after the cursor with
      an older payload timestamp (`eln/adapter.py` helper + both adapters). Tests: `test_eln.py`.
- [x] **A2** Deployment docs: `CHEMCLAW_ENTRA_REQUIRED` for exposed deployments, removed
      `CHEMCLAW_ENTRA_CLIENT_ID`; comment pinning the background worker at `replicas: 1`.
- [x] **A3** Eval-drift alert visibility — WARNING at emission + documented read procedure.
      Tests: `test_eval_drift.py`.

### Wave B — real defects [S]–[M]
- [x] **B1** Substructure matching off the event loop (`asyncio.to_thread` + wall-clock bound,
      `substructure_match_timeout_seconds`). Tests: `test_molfp.py` incl. loop-responsiveness.
- [x] **B2** Emit `PlanEvent` + `JobStartedEvent` (both currently dead types; closes D-042).
      Ambient job sink + changed-only plan emission. Tests: `test_runner.py`/`test_service.py`. ADR D-077.
- [x] **B3** Supersede memory notes on cluster merge/shrink (`memory/supersede.py`, bi-temporal
      `valid_to` + plain-text replacement id). Tests: `test_memory.py`. ADR D-078.

### Wave C — needs a decision first
- [x] **C1** Workflow-versioning policy doc + deploy checklist (no CI guard, on purpose). ADR D-079.
- [x] **C2** Chemical safety screening, minimum viable slice — implemented under the plan's stated
      defaults (advisory-only, committed SMARTS table, hard-failing `kg-validate` gate). All three
      are config, so reversing any of them is an env change. ADR D-080.

## Verification
- One commit per item; `make lint type test` green after each.
- CHECKMATE (G1–G7) after wave B, and again after C2 — B3 and C2 both touch the GxP surface.
- `BACKLOG.md`/`DEFERRED.md` updated at the end of each wave.

## Review — implementation (all eight items shipped)

Seven commits, each green under `make lint type test`; the suite went **624 → 684 passing**
(41 offline skips unchanged). One commit per item, revertable on its own.

| Item | Commit | What landed |
|---|---|---|
| A1 | `d23d2fd` | Late-arriving ELN exports warn by name instead of vanishing (`eln/adapter.py` helper, both adapters) |
| A2+A3 | `7fb9fdb` | Boot-blocking settings documented; drift alerts logged at WARNING; three stale runbook statements corrected |
| B1 | `cf13334` | Substructure matching moved to a worker thread under `substructure_match_timeout_seconds` |
| B2 | `f2e083a` | `PlanEvent` + `JobStartedEvent` emitted (D-077) — the two dead event types are now live |
| B3 | `6d96da2` | Memory notes retired on cluster merge/shrink (D-078) |
| C1 | `b2ea2d4` | `docs/workflow-versioning.md` + deploy checklist (D-079) |
| C2 | `744c265` | Advisory hazard screening: rule table, tool, skill, `kg-validate` gate, recall metric (D-080) |

**Decisions taken during implementation, not in the plan:**

- **B3's supersede predicate is `valid_to is None`, not `is_current`.** The plan said "still
  current"; that made the future-`valid_from` clamp unreachable dead code and left the idempotence
  argument implicit. Testing for an unset end date is simpler, makes re-runs provably idempotent,
  and covers not-yet-valid notes.
- **B2 announces only genuine launches.** The idempotent re-submit branch returns a possibly
  already-completed job that will never push back; announcing it would leave a permanently
  "running" row in the UI.
- **C2 shipped under the plan's default answers** to its three open questions rather than blocking,
  since each is config-reversible and the gate has nothing to break today (no procedure notes in
  the corpus). Flagged for confirmation below.
- **Rule-table SMARTS were verified against parsed molecules, not read.** Perchlorate and
  permanganate needed `~` (any-bond) patterns because RDKit sanitizes them to charge-separated
  forms — a double-bond pattern would have silently never fired.

**Guards that fired and were fixed properly, not around:** `SafetyRulesError` had to join the
non-retryable bad-data list (`test_publish.py` walks every `ChemclawError` subclass), and
`screen_hazards` had to be added to the expected in-process tool set (`test_tool_registry.py`).
Both existed to catch exactly this kind of omission.

**Still open for the user** (unchanged from the plan's §5): confirm C2's scope defaults; decide
whether to give the Neo4j tipping point a measurable trigger.

## Review — assessment pass

The three findings worth flagging:

1. **Six backlog lines were claims about the tree that are no longer true** — the largest being
   F4-T5's "per-request role scoping" open item, which D-052 delivered. Assessing before building
   removed more work than it added.
2. **Two "deferred polish" items are actually defects**, and both were sitting under headings that
   read as done: the substructure match blocks the shared event loop (B1), and memory cluster
   merge/shrink leaves stale notes as current knowledge with no supersede link (B3).
3. **The one genuinely large item (C2, safety screening) is the one the user parked for a decision**,
   and its own precondition — "decide before a phase that could propose a hazardous procedure" — is
   already past, since BO recommendations and development reports both publish procedures today.

**Method note for next time:** two mutation "survivors" were mis-targeted patches (one replaced a
docstring, not the guard) and two more only survived because I ran a narrow test file instead of the
suite. Every survivor was re-verified against the full suite before being reported; nothing went
into the findings table on the strength of the first run. Worth keeping as the default discipline —
a false finding costs more than a missed one.

---

# Config-extensibility backlog — items 5–7 (completing the audit backlog)

Source: `docs/audit/10-config-extensibility.md` §9. Items 1–4 landed earlier (`b07a2b2`, `76c03b2`,
`4884024`, `024105d`; ADRs D-075/D-076). Items 5–7 were BACKLOG-gated on triggers that had not
fired; completed on instruction. ADR **D-081**.

- [x] **6. [S] MCP transport union** (`6390f91`) — `StdioMcpServerSpec | HttpMcpServerSpec`
  discriminated on `transport`; `_mcp_tool` dispatches to `MCPStdioTool`/`MCPStreamableHTTPTool`,
  `assert_never`-exhaustive. **Callable `Discriminator`** reads a missing tag as `stdio` so every
  pre-union config keeps working — a plain `Field(discriminator=…)` would have broken every
  deployment at startup. `allowed_tools` is transport-independent, so the PR-gate boundary is too.
- [x] **5. [S] Skill manifest + enable-list** — `agents/skill_manifest.py` (`SkillManifest`,
  `extra="forbid"`) + `EnabledSkillsSource` + `settings.skills_enabled`. `make skill-validate` now
  validates frontmatter *and* checks declared `tools`/`mcp_servers` against the live registries;
  four shipped skills declare their real deps. Empty enable-list = today's behavior.
- [x] **7. [S] Config idiom house rule** — recorded in `config.py`'s module docstring; no field
  migration (churn without a defect).

## Review

Gate green after the cluster: ruff + `mypy --strict` clean over 234 files, `make skill-validate`
passes, full suite **631 → 650 passed** (19 new tests), 41 offline-only skips.

**The item-5 payoff is the dependency check, not the schema.** A manifest that merely typed
`name`/`description` would be ceremony. What earns its place is that a skill can now *declare* the
capabilities its judgment is written about, and the gate verifies them against the live tool
registry (D-075) and `settings.mcp_servers`. Verified by deliberately renaming a declared tool:
the gate fails with the exact name and exits non-zero. That closes a real hole — a skill teaching
a deleted tool previously survived as plausible, stale prose that nothing could detect.

**The item-6 risk was backwards compatibility, not the union.** Every shipped config is untagged;
a textbook `Field(discriminator="transport")` rejects untagged payloads, so the "clean" version of
this change would have broken `.env.example`, the Helm values, and every deployment at startup. The
callable discriminator defaulting to `stdio` is the whole reason the change is safe to ship.

**Invariant held (audit §7).** Both new narrowings attenuate and neither authorizes: the enable-list
cannot advertise a skill no directory provides and `RoleScopedSkillsSource` still runs on top; a
manifest's declared tools are documentation the gate validates, never a grant — `enforce_tool_authz`
is untouched. Fail-fast was placed by blast radius: an unknown enabled-skill name fails the
pre-deploy gate rather than raising per turn, since a config typo must not break live conversations.

**Rule-of-Three note.** These two items were trigger-gated and the triggers had not fired; they were
built on instruction. Both are honest rather than speculative — item 6 is a real second variant with
working dispatch, item 5's check has four real declaring skills. What *would* have been speculative
stayed out: no HTTP server is configured, and profile Stage 3 (filesystem-discovered profiles)
remains deferred.

**Still open, deliberately:** the deep-analysis items DA-5 and DA-10 are marked "needs a decision"
(cache staleness policy; how much live-edge risk to buy down offline) — those are judgement calls
for sign-off, not implementation work, per the audit's do-not-self-resolve convention.

---

# DA-5 + DA-10 — the two decision-gated findings, signed off and implemented

Source: `docs/audit/12-deep-analysis.md` §"Decisions needed" (D-1, D-2). ADR **D-082**.

- [x] **D-1 / DA-5 — graph-cache TTL.** `graph_cache_ttl_seconds` (default 5.0) skips the O(notes)
  stat scan inside the window; `kg.graph.invalidate_cache()` is the bust hook and the PR-gate
  submitter calls it. Measured **164 ms → 0.52 ms** warm query at 10k notes (this sandbox's disk;
  the audit measured 75 ms scan on faster storage — same shape).
- [x] **D-2 / DA-10 — Helm render gate.** `make helm-validate` (`helm template` | `kubeconform
  -strict`, OpenShift `Route` via the CRD catalog) wired into CI, plus `tests/test_helm_chart.py`
  for the gap a schema check cannot see.

## Review

Gate green: ruff + `mypy --strict` clean over 242 files, full suite **710 → 719 passed** (9 new tests), 41 offline skips.

**The TTL's cost is real and was measured, not assumed.** Four existing tests had to pin
`graph_cache_ttl_seconds = 0`: two assert fingerprint-based busting, two assert *disk-authoritative*
reads (a deleted note must not be cited; an on-disk corpus edit must invalidate the eval memo).
That is the change being visible exactly where it should be. I considered special-casing deletions
so a retracted note could never be served from cache, and rejected it: it would close the delete
case but not the *edit* case (a corrected note is cached just the same), giving an inconsistent
guarantee and false comfort. The uniform window is the honest contract. It also does not weaken the
stale-index guard in production — that guard compensates for a derived index rebuilt by a
background job, whose staleness is minutes-to-hours, so seconds are noise against it.

**DA-10's real find was the gap kubeconform cannot cover.** A schema check validates *Kubernetes*
shape; it cannot know whether `CHEMCLAW_FOO` is a real setting. Two failure modes lived there:
a key that is not a field (pydantic-settings **tolerates** an unknown prefixed *env var* — unlike
an unknown key in a `.env` file, which is what broke the quickstart in DA-1 — so it is silently
ignored, which in a GxP deployment is worse than a crash), and a malformed value on a real field
(crashes every pod at import). Both are now caught offline and both were **mutation-verified** —
inject the fault, watch the suite go red, restore.

**Verified, not assumed:** I checked empirically whether an unknown `CHEMCLAW_*` environment
variable crashes `Settings()` before designing around it. It does not — the `extra="forbid"` bite
is specific to `.env` files and kwargs. Had I assumed symmetry with DA-1, the parity test would
have asserted a crash that never happens and the *actual* defect (silent no-op) would have stayed
uncovered.

**Incidental finding:** `CHEMCLAW_COMPONENT` is set on every Deployment, is not a `Settings` field,
and nothing in the app reads it. Harmless, plausibly useful for `kubectl describe`, so it is
allow-listed **by name** in the parity test rather than the check being loosened — any other
non-field key is a real finding.

**Not verifiable here:** `make helm-validate` itself cannot run in this sandbox (no `helm`,
no `kubeconform`, no network to fetch them). It will execute for the first time on CI. The offline
chart tests are what I could and did prove.
