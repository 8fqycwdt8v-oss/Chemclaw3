# D-059 — F10-E/B: per-task model routing + answer verification & confidence routing (D-A11)

**Context.** A capability comparison against a commercial pharma-agent *platform* (IntuitionLabs)
found Chemclaw at or ahead on the durability/identity/audit spine, with deltas in retrieval breadth,
output verification, fine-grained authz, orchestration topology, and metrics polish. Phase F10
(`docs/parity-plan.md`) closes the ones that add value now and records triggers for the deferred
ones. Two of those deltas: no per-task model selection, and no verifier/confidence on the answer
path (only the report's deterministic citation gate).

**Decision.**
- **F10-E:** `build_chat_client(task="agent")` consults `settings.model_routes` (JSON task→model),
  falling back to the provider default. Still the single import site for a chat client — a task is a
  per-model choice on the one internal endpoint, not a second provider.
- **F10-B:** `agents/verifier.py::verify_answer(answer, evidence)` scores citation faithfulness and
  returns a `VerificationResult` (per-claim `ClaimCheck` + aggregate `confidence`). When
  `verifier_enabled`, an LLM-as-judge runs on the cheap routed `"verifier"` model via structured
  output; otherwise the deterministic report gate (`report.harness.verify_claims`) is the offline
  fallback (DRY, one citation check). `verify_turn_answer` resolves an answer's `[[wikilinks]]` to
  the notes it cites — the conversational scoring input. The runner stamps `AnswerEvent.confidence`
  + `unsupported_claims`; a low-confidence answer surfaces a review affordance and routes to the
  existing D-032 hold. No new gate primitive; a verifier failure degrades to the unscored answer.

**Consequence.** Default-off: `model_routes={}` and `verifier_enabled=False` reproduce today's
single-model, unscored-answer behavior exactly. The durable report workflow verifies at citation
level (it has no synthesized prose); the conversational path gets the LLM faithfulness score.

**Result.** `make lint type test` green. Tests: `test_llm_provider`, `test_verifier`, `test_runner`,
`test_config`.
