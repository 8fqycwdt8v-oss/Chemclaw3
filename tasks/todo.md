# Per-actor turn admission, and the register repairs beside it — plan

Opened after a harness audit of this tree against the eight-component agent-harness taxonomy
(agent state, tools, planning, memory, orchestration, context management, safety, observability).
All eight are present. The audit named four gaps; this closes the one that is a defect and
registers the one that is a decision.

## What was in scope and what was not

- [x] **Per-actor concurrent-turn cap** — a real gap, registered nowhere, closeable and verifiable
      offline. Done.
- [x] **Three dangling cross-references** — pointers to a row, a symbol and a backlog entry that do
      not exist. Done.
- [ ] **Tool-schema deferral** — *deliberately not built.*
      `D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for` ships `accepted (design only)`
      with four conditions; the fourth is a stop ("if the eval corpus cannot separate the two arms
      here either, the honest outcome is to leave the schemas bound"), and the third demands the
      deferred arm be shown to match the bound arm *on tool selection*. That needs a live model.
      `printenv 'API-KEY'` is absent here and `CHEMCLAW_LLM_BASE_URL` is unset, so building it
      would ship exactly the unmeasurable control the ADR forbids. **Registered as a
      `DEFERRED.md` row instead** — which is also the repair for dangling reference #1.
- [ ] **The delegation experiment** (`BACKLOG.md` #359) — not folded in. Its offline half (a
      `no-helper` profile, a runner recording `delegated`, a `--suite delegation` entry point) is
      buildable and is the natural next change; the run itself needs the same absent gateway, and
      `mock_llm` cannot decide whether to call `task`, so the arms would differ by script rather
      than by model behaviour.
- [ ] **Flipping `tool_authz_default` / `agent_memory_enabled` / `embedding_provider` /
      `data_sources`** — each re-opens a merged ADR. `D-2026-09-13` and `D-2026-09-16` establish
      the shape such a re-opening takes; each is its own decision, not a rider on an admission fix.

## Part 1 — the cap

- [x] `TurnLease.actor`; `_claim_turn_slot(..., *, actor)` keyword-only with no default;
      `_start_turn_lease` carries it across the restamp; new `_actor_turns_in_flight`.
- [x] Route check **above** `_claim_turn_slot`, 429, `besides=session_id`.
- [x] `actor=None` at the two maintenance holds (fork, delete).
- [x] `service_max_concurrent_turns_per_actor` (default 0); chart sets 4 against 12.
- [x] `chemclaw_turns_refused_actor_cap_total` (unlabelled) + `chemclaw_turn_actor_capacity` gauge.
- [x] `D-2026-09-19-a-pod-wide-cap-is-not-a-fair-one` + ledger row + topic-table row.
- [x] Tests: `tests/test_turn_fairness.py` (10), `test_detach.py` (1), `test_stream_contract.py` (3),
      `test_deploy_chart.py` (1).

## Part 2 — the register repairs

- [x] `DEFERRED.md` gains the tool-schema-deferral row `D-2026-09-04-…:93` already claimed existed.
- [x] `BACKLOG.md:547` cited `MINIMUM_COMPARED_SHARE`, deleted from `evals/delegation.py`.
- [x] `api/rate_limit.py`'s docstring pointed at a backlog row `D-2026-08-01-a-per-process-cap-…`
      closed and deleted; repointed, and the half that ADR did *not* close is now said out loud.

## Review

**The design decision that mattered** was not to copy `routes/streams.py`. Its per-user stream cap
is a `dict[str, int]` with a release closure, which is right there and wrong here: `_claim_turn_slot`
documents a window where the turn's generator is never advanced and no `finally` runs, so a plain
counter would stay incremented for the pod's lifetime and refuse that human until a restart — the
guard bricking exactly the chemist it protects. Deriving the count from `TurnLease` inherits the
lease's expiry, its identity-checked release and its existing sweep, and adds no `app.state` field
at all.

**Two decisions deliberately contradict an adjacent precedent**, which is why this got an ADR rather
than a commit message: it refuses at the handler top with 429 where D-166 moved the neighbouring
admission decision onto the stream (what D-166 moved was a *wait*; this is a refusal), and it does
**not** release the slot at a detach although `detach.py` releases the permit (a detached turn has
no waiting client, but it is still spending the actor's share — releasing would hand
POST-and-hang-up back its unboundedness). Both are pinned by tests that go red if someone later
"tidies up" the inconsistency.

**Measured rather than argued**, per CLAUDE.md. With the route guard disabled and the cap set to 2,
alice's third concurrent turn was **admitted and streaming** (`active_turns=3`). With the guard
restored, the same request answered **HTTP 429**. The first of those is what every deployment runs
today.

**What this does not fix, stated rather than implied.** The cap is per process, so at
`maxReplicas: 6` one principal can still hold 6 × 4 = 24 concurrent turns across the fleet. That is
better than 6 × 12 = 72 and is not a per-actor guarantee. The fleet-wide form belongs at the ingress
(SCALE-1) and the ADR carries the `Revisit when:` naming the file an ingress policy would land in.

**A guard I did not know about caught me**: `tests/test_decision_log.py` requires a new ADR to be
cited by a row of the README's "By topic" table, not merely listed in the ledger. Filed under
*Front door / SSE contract*, beside D-166.

## What four fresh-context reviews found after the first push

Worth recording because the pattern is not "sloppiness": every finding was a place where the prose
was more confident than the code, which is the failure this repository is organised around.

- **The central correctness argument was half true.** "Deriving the count from `TurnLease` inherits
  the lease's expiry" holds in the *streaming* phase and not in the *reservation* phase, where
  `deadline=math.inf` and three untimed store round trips run. Driven with the owner store parked:
  the actor was still refused after 7.5× the widest lease that configuration can stamp — a
  permanent lockout across every session, the exact brick the design claimed to prevent.
  `claimed_at` + `_still_holding` close it; the session's own 409 is untouched.
- **The pointer-repair commit added a dangling pointer of the class it was closing.** The ADR cited
  `D-2026-09-05-a-lease-is-demand-and-a-permit-is-occupancy`, which has never existed. It was
  copied from `tests/test_detach.py:344`, where it was already dangling. Both fixed. Nothing
  catches this automatically: `test_docstring_paths` exempts `docs/decisions/`, and
  `test_decision_log` checks *test* citations rather than ADR-to-ADR ones.
- **"Two orders of magnitude" was one**, and the two quantities (requests/minute, simultaneous
  turns) are not commensurable anyway — five sites, all replaced with the honest form.
- **`live_storm`'s family A sweeps the *admission* cap**, not this one, which did not exist when it
  was written — six sites.
- **The invariant was enforced against `values.yaml`, not against the running config**, so a
  `--set` or env override shipped a guard that refuses nothing while publishing a gauge that reads
  as protection. Now a cross-field validator.
- **The cap inverted under the shared dev principal**: with `entra_required=false` every caller is
  one oid, so "per actor" meant "per pod" and one client refused everyone. It skips itself there.
- **A sibling bug found and fixed rather than noted**: `routes/streams.py`'s 429 carried no
  `Retry-After` either, so the same UI misclassification (`budget_exhausted`, composer locked)
  applied to `/events`. Hardening one route and leaving the other would have been knowing about it.
- **The log reported the configured cap where it claimed to report a measurement**, which would
  have hidden a count *above* the cap — the one observable symptom of a lease outliving its turn.
- **Nothing asserted the counter increments**, so the dashboard panel could have read zero for ever
  while every test stayed green.
