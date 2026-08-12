# M12 · plan → approve → execute, live

The GxP gate as a *conversation*: a write refused before approval, the same write running after it, and the plan changing out from under the decision (DARK-1).

- front door `http://127.0.0.1:8000`
- 28 state-changing tool(s) the gate governs
- transcripts in `tasks/live-test/transcripts/plan-gate`

| probe | check | result | observed |
| --- | --- | --- | --- |
| pg-01 | a plan a human can decide on | PASS | 4 plan item(s), hash 6e74b1bd9c3b |
| pg-01 | an unapproved state-changing call is refused | PASS | refused ['compute_reaction_energy']; ran - unrefused |
| pg-01 | the decision was accepted | PASS | POST /sessions/…/plan/decision → [204] |
| pg-01 | the approved plan executes | PASS | ran ['compute_reaction_energy', 'compute_reaction_energy', 'compute_reaction_energy', 'propose_knowledge_note', 'propose_knowledge_note']; refused - |
| pg-01 | a changed plan is re-gated (DARK-1) | PASS | plan hash 6e74b1bd9c3b → 51bc29a442dd (new identity), approved=False, ran - under the earlier decision |

**5/5 checks passed.**
