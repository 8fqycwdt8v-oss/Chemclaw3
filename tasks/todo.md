# Agent-authored skills under a human gate

Plan: personal tier on, an admin promotion path, an organisation-wide tier.
Full design: `/root/.claude/plans/atomic-zooming-hopper.md`.

## PR 1 — the org tier, dark

- [x] 1. `PermittedStoreBackend`: one `permits` predicate for both store-backed tiers
      (closes `BACKLOG.md:85`, the `/mine/` prompt-only narrowing)
- [x] 2. `agent/org_skills.py` — the tier, its two namespaces, its caps
- [x] 3. Mount `/org/` (`scratchpad.py`) and advertise it (`langgraph_agent.py`)
- [x] 4. `api/routes/org_skills.py` — six routes, three behind `_is_reviewer`
- [ ] 7a. Tests for 1-4
- [ ] 8a. ADR 1 (blast radius) + ADR 2 (revert is a pointer)
- [ ] 9a. ARCHITECTURE/BACKLOG/README rows

## PR 2 — the flip (held for review)

- [ ] B1. `_observed_prefix` builds under `session_store="postgres"`; re-measure `CEILINGS`
- [ ] B2. migrate-job: store setup between migrate and grants
- [ ] 5. `agent_memory_enabled = True`, `.env.example` parity, the two new caps
- [ ] 6. `values.yaml` states the posture
- [ ] 7b. Ratchet + config + chart tests
- [ ] 8b. ADR 3 (a tier every prefix pays is still not a ceiling)

## PR 3 — `Chemclaw3_ui`

- [ ] 10. Review queue, personal skills manager, org admin view

## Review

(filled in at the end)
