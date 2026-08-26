**Failed approach, recorded so it is not retried.** Driving `build_langgraph_agent()` in-process
against the live API to measure real turn economics: this environment's credential is rejected, and
clearing the session's `ANTHROPIC_BASE_URL` gives the same 401. `cli.mock_llm` makes the *harness*
runnable end to end but cannot stand in for judgement — it emits scripted tool calls without
choosing them, measured at expected-tool-reached 0/3. W2.3 and W2.4 need a real credential and
nothing else.


---

# Review of the merged #215, and the six defects it found

`/code-review` against `410f494` returned eight findings; all eight were verified by running the
code rather than by reading it, which changed the ranking twice.

## Fixed here

- [x] **The credential probe aborted the entire suite at collection.** `skipif` evaluates at
      collection time, so an unreachable provider raised there and pytest reported `Interrupted`.
      Measured: 0 tests ran from two files, one of them the static-prefix ratchet. Now a fixture;
      measured after: 19 passed, 2 errors scoped to the two live tests. An AST test pins the
      absence, because there are several ways back in.
- [x] **A 30,000 default became a hard minimum for `agent_context_token_budget`.** A deployment
      setting only the budget to 20,000 could not construct `Settings()`, citing a variable it never
      set. The default is clamped; the refusal is kept for an *explicit* over-budget value.
- [x] **`forget_calls()` was global.** Clearing preserves the newest tool results, so the blanket
      reset forgave repeats the model can still read — once per reduction. Now it names the calls
      whose results were actually replaced, read off upstream's `context_editing.cleared` stamp.
      Neither module had a test for the coupling; both do now, plus the marker in the upstream
      register.
- [x] **`len(GRANDFATHERED) <= 18` could not say "only shrinks".** The first fix — a frozen copy —
      was worse: derived from the same literal, it can never differ, and it **passed against a
      deliberately planted addition**. The literal is now a dated baseline and the live set is
      computed from it.
- [x] **Two deepagents privates were unregistered**, one of them the arity dependency D-2026-08-14
      removed from production. Both now in `tests/test_upstream_surface.py`.
- [x] **`processes.sh` persisted its env before minting the fleet's tokens**, reintroducing the
      exact failure the comment above the write warns about.

## Corrected rather than fixed

- [x] **`turn_cost_ratio` scores a fixture.** Its case is all literals, so the metric is a constant
      of committed data — the claim that it "moves only when the system does" was false of this
      case. Both the case and the docstring now say so; the rewiring is a backlog row, blocked on a
      deployment that has turns in it.

## Deferred with a reason (backlog rows)

- [ ] The run-directory collision between `processes.sh` and `e2e-full-stack/up.sh` — a decision
      about which lane owns the fleet, not an edit.

## What the review overturned

One finding was ranked most severe and measured as overstated: the compaction trigger counts
*messages* only (`token_count_method="approximate"` passes no system prompt and no tools), so 30k is
a long conversation rather than the "mid-sized thread" claimed, and the 24,838-token static prefix
does not count toward it. The underlying imprecision was real and is fixed above; the severity was
not. Worth recording because it is the second time this session that the articulate explanation and
the true one came apart — and running it is what separated them.
