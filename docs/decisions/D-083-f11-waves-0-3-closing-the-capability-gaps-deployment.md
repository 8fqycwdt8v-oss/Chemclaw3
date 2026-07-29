# D-083 — F11 waves 0–3: closing the capability gaps (deployment, reachability, chemistry)

**Phase F11 wave 0–3: closing the capability gaps found in `docs/audit/12-capability-gap-analysis.md`.**

**Context.** A whole-codebase completeness sweep asked "what does this system need that nobody has
listed yet?" — a different question from the AG-*/KM-* gap docs, which checked named capabilities
against a checklist. The answer reframed the priority order: the engine is sound (as those docs
concluded), but the seams *around* it had three classes of hole, and the sharpest ones were
capability the repo had already paid for and could not use.

**Decision (what was built, and the reasoning that shaped each).**

1. **The chart could not run the knowledge layer in either direction.** Readers resolve
   `knowledge_dir` as a local path; the chart mounted no volume and ran no sync, so a merged note
   never reached a live pod. `GitNoteSubmitter` needs a push credential; the chart declared three
   secrets, none of them git. Fixed with a clone-or-refresh replica (init container + sidecar) and
   a separate writable submitter clone. The two are deliberately **different directories**:
   `git checkout -B note/<id>` switches a whole working tree, so the submitter cannot share the
   tree readers are reading. Refresh is `fetch`+`reset --hard`, never `pull` — a read replica must
   not be able to land on a merge conflict.

2. **The image was missing what the running components read.** `skills/`, `scripts/`, `evals/` and
   `knowledge/` were never COPYed and `git` was never installed. In-cluster this meant the agent
   advertised *no skills at all*, no Temporal Schedule could ever be created (nothing ran
   `scripts.schedules`), and the PR-gate could not shell out to git — three silent capability
   losses, none of which fails a test or a lint. `tests/test_deploy_chart.py` now gates image
   completeness, include/values resolution, control-flow balance, and entrypoint dispatch offline;
   F6's "offline-verified" check had confirmed the chart was *well-formed*, not *sufficient*, and
   that distinction is the whole lesson.

3. **Three finished subsystems had no caller.** `DevelopmentReportWorkflow`, `BoCampaignWorkflow`,
   and the human half of `InteractionApprovalWorkflow` were implemented, tested and
   worker-registered, reachable only from the Temporal CLI. The repo's rule is "no abstraction
   without a second caller"; this is the inverse failure — a complete implementation with zero —
   and it is worse than absent because the backlog marks the phases complete. The approval decision
   is an **HTTP route, never an agent tool**: a tool would let the agent approve its own candidate
   and collapse the GxP line the PR-gate exists to draw.

4. **Prose promised capability the code lacked.** Two independent findings turned out to be one
   defect class: a skill directing the agent at `BoCampaignWorkflow` (uninvocable), and
   `_INSTRUCTIONS` advertising impurity answers with no schema field. `make prose-validate` gates
   it and immediately found a third, live instance — `deep-research/SKILL.md` taught three tool
   names (`find_similar_*`) that differ from the agent's actual MCP tools and would have failed at
   call time. This is the *deterministic half* of the AG-13 behavior eval, and unlike AG-13 it needs
   no live LLM, so the AG-13 deferral never covered it.

5. **Chemistry the prompt already promised.** `performed_at` gives the largest note class a time
   axis and finally feeds F10-G2's bi-temporal fields; `purity_percent`/`impurities` make the
   advertised impurity answers possible. A test pins that none of it reaches `reaction_smiles()` —
   feeding structure would have changed every DRFP fingerprint and silently invalidated the
   structural index. `resolve_compound` bridges names to structures (and unblocks the deferred
   per-step species linking, whose own trigger was "a name→SMILES tool exists"). `screen_hazards`
   is the safety layer `BACKLOG.md` said to scope "before any capability phase that could propose a
   hazardous route" — that phase had shipped.

6. **Two refusals are as load-bearing as the additions.** Retention prunes spent operational rows
   but **refuses** `audit_events` (deleting from a hash chain is indistinguishable from the
   tampering it detects; safe disposal needs archive-then-reseal, a GxP design decision for its own
   ADR with QA sign-off) and `calculation_results` (age is the wrong axis for a cache — D-011 makes
   eviction a silent recomputation, potentially an HPC run). Similarly, `screen_hazards` reports
   `unresolved` species as prominently as findings, so a clean report cannot read as a clearance.

**Correction recorded.** **AGT-1 ("no turn cancellation") was withdrawn as a false finding.** The
claim — that an abandoned turn holds its admission permit and never books its tokens — rested on a
`grep` for `CancelledError` returning nothing. That was true but not load-bearing: the handling is
structural (sse-starlette closes the generator; the front door's and runner's `finally` blocks
release the permit and book the budget), and was already correct as of `4bc9b04`.
`tests/test_turn_cancellation.py` measures it and is kept, because nothing previously *proved* the
behavior and a plausible refactor (an `await` in the runner's `finally`) would reintroduce exactly
the leak that was alleged. The analysis document records the withdrawal rather than quietly
dropping the row.

**Consequences.** Six new config groups (all default-off where they change a path), three new
Schedules (reindex, retention, gated on explicit opt-in), one new skill (`process-safety`), one new
CI gate (`make prose-validate`), and the chart is deployable. W3's remainder (metrics, schedule
health, mid-turn resume) and all of W4 stay open and are listed in `BACKLOG.md` — scaling the work
down mid-wave is the user's call, so the boundary is recorded rather than blurred.
