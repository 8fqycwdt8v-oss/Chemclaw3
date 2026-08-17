# Refutation pass — sweep-dynamic-resolution (lens: does it actually reproduce?)

Scope: only **critical**/**high** findings. The file contains exactly one — finding 1 (high).
Findings 2, 3, 4, 7 are medium and 5, 6 are low; all six are out of scope and were not verified.

## 1. A profile file's `harness_autonomy` is an unvalidated string, so a typo silently turns the plan gate off

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**: I did not run the reporter's script or use their `/tmp/prof` files. I re-derived
  the claim from source and wrote my own repro.

  First, the cited symbols are real and current at the cited lines (`git log --oneline -1` =
  `581e3982`, working tree clean apart from other verdict files):
  - `src/chemclaw/agent/profiles.py:55` — `harness_autonomy: str | None = None` (no `Literal`, no
    validator; `model_config = ConfigDict(extra="forbid", frozen=True)` constrains keys only).
  - `src/chemclaw/agent/plan_gate.py:130` `PLAN_ONLY = "plan_only"`, `:140 autonomy_for`,
    `:178 return harness_enabled_for(profile) and autonomy_for(profile) == PLAN_ONLY`.
  - `src/chemclaw/core/config/agent.py` — `harness_autonomy: Literal["plan_only", "execute"]`.

  Consumers of `gate_applies` (`grep -rn` over `src/`): exactly two —
  `agent/langgraph_agent.py:630` (`*([enforce_plan_approval] if gate_applies(profile) else [])`)
  and `api/runner.py:166`. Nothing else re-checks the value.

  My own profile dir `/tmp/vprof` with four files (`typo.yaml` = `harness_autonomy: plan-only`,
  `honest.yaml` = `plan_only`, `nounset.yaml` = `harness_enabled: true` only, `badkey.yaml` =
  `harnes_autonomy: plan_only`), loaded via `CHEMCLAW_PROFILES_DIR=/tmp/vprof`:

  ```
  $ uv run python /tmp/vprof/repro.py      # with badkey.yaml present
  LOAD RAISED: ProfileError /tmp/vprof/badkey.yaml: invalid profile: 1 validation error
    harnes_autonomy  Extra inputs are not permitted [type=extra_forbidden, ...]
  ```
  — so `extra="forbid"` does refuse a misspelled *key*, exactly as the finding says, which sharpens
  the contrast rather than softening it. With that file removed:

  ```
  $ uv run python /tmp/vprof/repro.py
  global harness_enabled: False harness_autonomy: 'plan_only'
  registered: ['default', 'honest', 'nounset', 'typo']
  typo     autonomy='plan-only'    enabled=True gate_applies=False
  honest   autonomy='plan_only'    enabled=True gate_applies=True
  nounset  autonomy='plan_only'    enabled=True gate_applies=True
  ```

  Then the consequence itself, not the predicate — I built the real governance chain for each
  profile (`langgraph_agent.tool_governance_middleware` / `_harness_middleware`):

  ```
  $ uv run python /tmp/vprof/mw.py
  typo     gate attached: False  harness mw: ['TodoListMiddleware', 'enforce_loop_cap']
  honest   gate attached: True   harness mw: ['TodoListMiddleware', 'enforce_loop_cap']
  nounset  gate attached: True   harness mw: ['TodoListMiddleware', 'enforce_loop_cap']
  ```

  And the asymmetry the finding rests its "fix" on is real:

  ```
  $ CHEMCLAW_HARNESS_AUTONOMY=plan-only uv run python -c "from chemclaw.core.config import settings"
  pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  harness_autonomy
    Input should be 'plan_only' or 'execute' [type=literal_error, input_value='plan-only', ...]
  ```

  Also checked: `grep -n profile Makefile` returns one line (`template-validate`, which resolves
  profile *names*), so there is indeed no target that validates profile field *values*; and
  `GET /profiles` (`api/routes/sessions.py:186`) serves `registered_profile_names()`, which my run
  shows includes `typo` as an ordinary selectable name.

- **Why**: Every element of the claim reproduces on the arguments stated. The line numbers and
  symbols are current, the trigger is a single hyphen in a file the discovery path accepts without a
  word, and the consequence is not merely the predicate flipping — the compiled middleware chain
  really is built without `enforce_plan_approval`, so every `side_effecting_call` (durable job
  launchers, `state_changing` connector tools, `/memories/` writes) executes with no approval check
  for sessions on that profile. Nothing logs it; `load_profiles` logs only "registered N profile(s)".

  Two things make it worse than the finding states, both visible in my run and neither mentioned:

  1. **The typo does not merely fail to request the gate — it destroys an inherited one.** In my run
     the deployment default was the shipped `harness_autonomy='plan_only'`, and `nounset` (which
     sets *only* `harness_enabled: true`) got the gate. `typo` did not. So the hyphen is an active
     downgrade of the deployment's default posture for that profile, not a missed opt-in; the
     finding's stated trigger ("under a deployment with `harness_enabled=true`") understates the
     reachable configuration set.
  2. The same profile still gets `TodoListMiddleware` and `enforce_loop_cap` (both keyed off
     `harness_enabled_for`, which *is* a validated bool). So the deployment sees a working harness
     that writes and displays plans — the surface a chemist reads as "this session is plan-gated" —
     with the enforcement silently absent. The failure is not visible from the outside.

  The only argument I can construct for downgrading is that the trigger is an operator authoring
  error in a trusted in-image file rather than anything attacker-reachable, and that no shipped
  profile sets the field today, so the defect is latent. I do not think that carries: this is a
  fail-open on a human-in-the-loop control whose sibling input (the env var) is validated, the
  mistake is silent at every stage (parse, register, build, serve), and the reporter's proposed fix
  — reuse the settings `Literal` — is one line with no behavioural cost. High stands.
