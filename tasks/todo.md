# Agent-authored skills under a human gate

Plan: personal tier on, an admin promotion path, an organisation-wide tier.
Full design: `/root/.claude/plans/atomic-zooming-hopper.md`.

## PR 1 — the org tier, dark

- [x] 1. `PermittedStoreBackend`: one `permits` predicate for both store-backed tiers
      (closes `BACKLOG.md:85`, the `/mine/` prompt-only narrowing)
- [x] 2. `agent/org_skills.py` — the tier, its two namespaces, its caps
- [x] 3. Mount `/org/` (`scratchpad.py`) and advertise it (`langgraph_agent.py`)
- [x] 4. `api/routes/org_skills.py` — six routes, three behind `_is_reviewer`
- [x] 7a. Tests for 1-4
- [x] 8a. ADR 1 (blast radius) + ADR 2 (revert is a pointer)
- [x] 9a. ARCHITECTURE/BACKLOG/SECURITY/README rows, `.env.example` parity

## PR 2 — the flip (held for review)

- [x] B1. `_observed_prefix` builds under `session_store="postgres"`; re-measure `CEILINGS`
- [x] B2. migrate-job: store setup between migrate and grants
- [x] 5. `agent_memory_enabled = True`, `.env.example` parity, the two new caps
- [x] 6. `values.yaml` states the posture
- [x] 7b. Ratchet + config + chart tests
- [x] 8b. ADR 3 (a tier every prefix pays is still not a ceiling)

## PR 3 — `Chemclaw3_ui`

- [x] 10. Review queue, personal skills manager, org admin view

## Review

**Shipped.** `Chemclaw3#423` and `Chemclaw3_ui#102`, same branch name in both.

The agent can now create and adapt skills, and activation is gated by a human whose identity
follows the blast radius: a chemist accepts what acts on their own turns, an administrator
publishes what acts on everyone's. `SkillsReadOnlyRefusal` was not relaxed anywhere — the agent
proposes and never writes.

### What the work turned up that the plan did not predict

- **Both stored tiers were narrowed in the prompt and not at the backend.** `BACKLOG.md:85` had
  it for `/mine`; `download_files` was ungated there too, which the reviewed tree had already
  closed and called "a latent hole rather than a live one". An org tier with the same gap would
  have been a *shared* corpus leaking past a profile narrowing.
- **The prefix ratchet measured a system nobody runs.** One predicate — `conftest.py` sets no
  `session_store`, the chart pins `postgres` — so `propose_skill` was stripped from every graph
  the ratchet compiled while the fleet paid 462 tokens a request for it.
- **A fresh install could not write to `store`.** Its tables are created at runtime, the grants
  naming them are a `pre-install` hook. Harmless while nothing wrote to `store`; the first-boot
  experience once the flip landed.
- **`<details>` renders its children whether or not it is open**, so the UI fetched every org
  skill's full version history — bodies included — on first paint.

### What the full suite caught that targeted runs did not

`.env.example` parity for two new settings, and `org_skills_read_only` arriving as an untriaged
`routed()` refusal. Both are exactly the class of defect the repo's derived guards exist for, and
neither would have shown up in the files I was editing.

### Measured, not argued

Prefix floor 69,872 of 70,600 (728 headroom, no cascade). `propose_skill` 462 tokens. Org tier
derived at 3,345 and **measured at 3,330** on its own mount — the derivation was carried from the
neighbouring tier and was wrong by 15, which is why the constant records both.

### Honest limits

- `test_no_adr_cites_a_commit_a_squash_will_strand` is red here and on the base commit: this
  session's clone is shallow, so two cited SHAs are not ancestors of the truncated `origin/main`.
- The UI's `e2e` lane could not run — Playwright wants a `chrome-headless-shell` this image does
  not carry. No spec touches the new screens; `ISSUES.md` carries the gap.
- 100 backend skips: helm, the `Chemclaw3-mcp` cross-checks, and three migration-additivity tests
  that need full history.
- `evals/delegation.py` still has never run against a model, and nothing here changed that.
