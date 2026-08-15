# D-2026-08-15-a-capability-that-ships-off-is-not-a-capability — the specialist team, the challenge panel and the routing measurement are deleted rather than left disabled

**Status:** accepted · **Date:** 2026-08-15 · Supersedes `D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks` and `D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared` in substance; retires the measurement `D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate` deferred its default to. Keeps `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`'s invariants as the constraint any future subagent must satisfy.

## Context

Three features shipped off and stayed off: the specialist team (`agent/team.py`, gated by
`agent_teams_enabled`), the challenge panel (`agent/challenge.py` + `agent/challenge_gate.py`,
gated by `challenge_enabled`), and the routing measurement built to decide whether the first should
ever be turned on (`evals/live.score_routing`, `data/evals/probes/m12/routing.yaml`, the
`live-routing` CLI arm and `live_routing_accuracy_min`).

Together that is **1,442 lines of agent code, about 400 more of eval machinery, 1,506 lines of
tests, a 271-line probe corpus, seven settings and three metric series** — none of it reachable in
any shipped configuration. `deploy/helm/chemclaw/values.yaml` sets neither flag; no profile
overrides them.

The instruction that prompted this was to make the harness lean and powerful. This is the part
where those two words do not conflict: nothing here contributes power, because none of it runs.

**The delegation question was never settled, and cannot be settled by this corpus.** D-2026-08-12
measured 2 of 15 delegations and found the cause structural rather than promptable — `reject_widening`
makes specialists ⊆ supervisor, so delegating is always a longer path to a tool already in hand.
D-2026-08-13 reframed the reason to spawn and re-measured: **14/15 against 14/15**, which the ADR
itself recorded as "the reframing changed nothing measurable". The old arm was already at ceiling,
and 14/15 against M12's 2/15 on the *same corpus* means the two harnesses were not measuring the
same thing. Neither number is a deployment's rate.

The corpus cannot be repaired into one either. `expects_specialist` is a single name, and
`BACKLOG:255` records that two of its fifteen probes genuinely span two specialists and fail in both
arms — so the accuracy figure has a floor of two unpassable probes before any model is involved.
Worse, `RoutingScore.accuracy` divides by *delegated* turns, so a supervisor that delegates once
and gets it right reads 100%.

## Decision

Delete all three, and the measurement with them.

**Deleted:** `agent/team.py`, `agent/challenge.py`, `agent/challenge_gate.py`,
`durable/answer_review.py`, `data/profiles/challenger.yaml`, `data/evals/probes/m12/routing.yaml`,
`evals.live.RoutingScore`/`score_routing`/`_score_self_answered`, the `live-routing` CLI arm and its
report, `Probe.expects_specialist`, the settings `agent_teams_enabled`, `challenge_enabled`,
`challenge_timeout_seconds`, `challenge_panel_size`, `challenge_quorum`, `challenge_max_attempts`
and `live_routing_accuracy_min`, the three `chemclaw_challenge_*` metric series, and the four test
modules plus twelve routing tests.

**`reject_widening` is deleted too, and that is the decision most worth arguing.** It is the
security invariant of D-2026-08-10 — a subagent's surface is an attenuation of its caller, never a
widening — and the instinct is to keep it against the day subagents return. That instinct is exactly
what produced `agent/identity/hpc_bridge.py::map_to_hpc_identity`: a control with zero callers, kept
alive by a test that calls it directly, whose own docstring explains it exists because "logging the
mapping is the compliance requirement". A guard that guards nothing is not a guard; it is a claim
that one exists.

The invariant is not lost, because an invariant is not a function. D-2026-08-10 is merged and
permanent, it states the rule, and `git log` holds the implementation. Whoever re-adds subagents
re-adds the check as part of doing so — which is when it will be tested against a real caller
instead of a test double.

**What is kept, and why:**

- **`HandoffEvent`** in the SSE union, and `ProbeOutcome.specialists` which is fed from it. Removing
  a member of that union is a coordinated change across `Chemclaw3_ui` and `Chemclaw3_mock`, and
  this is not that change.
- **`AnswerEvent.challenged` / `.review_hold_id`**, now permanently at their defaults. Same reason,
  and this is the one shape this repository otherwise forbids — a declared field nothing writes
  reads as coverage while proving nothing. It is therefore on a deadline rather than left to be
  rediscovered: both go in the cut that moves the transcript route off `session_messages`, which is
  already coordinated across the three repositories.
- **`verifier.score_answer` stays where it is.** It lived apart from `api/runner_answer` because two
  callers needed one verdict and neither could own it; one of those callers was the challenge gate.
  With one caller left the split is no longer load-bearing, and the docstring now says so rather
  than continuing to assert a second entry point that does not exist.

## Consequences

- Layer 1 loses about 12.5% of its lines and none of its behaviour. No shipped configuration changes.
- The `m12` probe directory keeps its plan-gate and degradation suites, which measure live features.
- **A future team is a new decision, not a resumption.** It needs a reason to spawn that survives
  contact with `reject_widening` — isolation and parallelism do, "reach a tool I lack" does not —
  and a measurement whose denominator does not depend on the model volunteering the behaviour being
  measured. `docs/planning/DEFERRED.md`'s row on narrowing a supervisor below its specialists is
  deleted with this ADR: the question it defers cannot be asked of a system that has no supervisor.
- **The self-critique capability the panel provided is not replaced here, and that gap is real.**
  `deepagents.RubricMiddleware` is the upstream shape for it — an LLM judge with a bounded revision
  loop, whose grader prompt already frames the transcript as untrusted observation, which is the
  injection objection that killed a summarizer three times in this tree. Adopting it is a separate
  change with two measured obstacles: its `after_agent` re-enters the same run, so its iterations
  are counted by `CappedModelCallLimit`'s `run_limit`, and its grader reads the transcript while
  `verifier.py` scores against the runner's `ToolCallTrace`.
