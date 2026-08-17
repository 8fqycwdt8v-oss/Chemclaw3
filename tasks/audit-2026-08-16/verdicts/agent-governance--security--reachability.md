# Verdicts — agent governance, security and hardening (lens: reachability + consequence)

Source: `tasks/audit-2026-08-16/findings/round1/agent-governance--security.md`.
In scope: the two findings marked **high**. The three marked medium/low are ignored by scope.

Working tree checked against `HEAD` before every measurement: `git diff HEAD --stat -- src/` is
empty and `grep -rn MUTANT src/chemclaw/agent/` finds nothing, so none of what follows is reading
another agent's mutant.

---

## An operator gate spelled with an empty role list opens the tool to everyone, including under the `deny` allowlist

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  Reproduced the stated repro exactly (`/tmp/v_f1.py`, real `Settings` built from env, real
  `authorize_tool`):

      $ uv run python /tmp/v_f1.py
      default: deny gates: {'compute_dft_energy': [], 'gather_evidence': ['Chemclaw.User']}
        compute_dft_energy: ALLOWED
        gather_evidence: refused (attacker-oid is not authorized to use gather_evidence: …)
        propose_knowledge_note: refused (… only an approved list of tools, and this one is not on it)

  Then looked for what the finding did not cite. `grep -rn tool_role_gates tests/` points at
  `tests/test_authz.py:227`:

      def test_an_operators_empty_role_list_opens_a_tool_rather_than_closing_it(...)
          """`tool_role_gates: {tool: []}` means "no role needed", and that convention is now pinned.
          Found by mutation testing (2026-08-04) … It is worth pinning precisely because the file's
          own comments state the *opposite* rule two lines away … The asymmetry is deliberate — an
          operator who writes `[]` against a tool has said something, whereas an unfilled chart
          default has not — but a deliberate asymmetry that no test can tell from an accident is one
          refactor from being "simplified" into a security change."""

      $ uv run pytest tests/test_authz.py -k "empty_role_list or non_empty_role_list" -q
      2 passed, 14 deselected in 2.76s

  The test body is the finding's repro (deny default, `{"find_notes": []}`, role-less actor,
  assert *allowed*).

  Checked the shipped posture: `deploy/helm/chemclaw/values.yaml` sets no `CHEMCLAW_TOOL_ROLE_GATES`
  and no `CHEMCLAW_TOOL_AUTHZ_DEFAULT` (only `CHEMCLAW_ENTRA_REQUIRED: "true"` and
  `CHEMCLAW_ENTRA_PRIVILEGED_ROLES: ""`), so out of the box the gate map is `{}` and the branch is
  never entered.

  Ran one variant the finding did not (`/tmp/v_f1b.py`), on the *default* `allow` posture with
  `entra_privileged_roles=Chemclaw.Admin` and `tool_role_gates={"propose_knowledge_note": []}`:

      DEFAULT_WRITE_TOOL_GATES: ['compute_dft_energy', 'propose_knowledge_note',
                                 'record_confirmed_answer', 'record_failure']
        compute_dft_energy: refused
        propose_knowledge_note: ALLOWED
        record_confirmed_answer: refused
        record_failure: refused

- **Why**

  The mechanism is exactly as reported and I could not break it: `[]` is not `None`, the explicit
  branch is taken, `_has_required_role(frozenset())` is `True`, and the `deny` default below is
  never reached. The sibling asymmetry is real too — `skill_access.RoleScopedSkills._permits`
  (`required is None or bool(get_current_roles() & required)`) reads the identical config shape as
  "nobody". And the `allow`-default variant above makes the exposure *wider* than the finding says:
  it needs no allowlist deployment at all, and an empty entry against a `DEFAULT_WRITE_TOOL_GATES`
  member is the one thing that can open a built-in write tool to a role-less user. That part the
  reporter missed and it is worth carrying forward.

  What does not hold is the framing and therefore the severity. Two things:

  1. **The trigger has no caller.** Trace outward from `authorize_tool` and the outermost entry
     point is not an HTTP request — it is the ConfigMap. Nothing an authenticated user, a model, a
     connector or a tool argument can do produces this state; it requires an operator to type a
     specific value into `tool_role_gates`. The shipped chart does not contain it, `.env.example`
     does not, and `tests/test_config.py:76` pins the default as `{}`. So there is no deployment
     that is exposed today, and no path by which one becomes exposed without a deliberate config
     edit. That is a footgun, not a live hole.

  2. **It is a pinned convention, not a silent accident, and the finding's evidence section reads
     as though it were the latter.** The finding cites the two comments at `authz.py:348-350` and
     `:377-384` as proof that "the same module applies the opposite rule twice, explicitly" —
     implying nobody noticed the third case. In fact the third case is the subject of a test named
     after it, written after mutation testing found the branch unasserted, whose docstring states
     the asymmetry *and its reason* ("an operator who writes `[]` against a tool has said
     something, whereas an unfilled chart default has not"). A reader of the finding alone would
     conclude the behaviour is unowned. It is owned; one can disagree with the convention, and I
     do, but that is a design argument, not a security defect report.

  Two consequences for triage. First, the proposed fix **fails the suite** —
  `test_an_operators_empty_role_list_opens_a_tool_rather_than_closing_it` asserts the exact
  behaviour the fix removes, so this cannot be applied as a bug fix; it has to be argued and the
  test rewritten with it. Second, the half of the fix that is unambiguously right and costs nothing
  is the config-side one: a `model_validator` on `EntraSettings` rejecting an empty list value in
  `tool_role_gates` makes the ambiguity unshippable while leaving the pinned convention intact, and
  it is exactly the shape of the validator already sitting at
  `core/config/entra.py:_entra_enforcement_is_configured`.

  Medium rather than high: real ambiguity in an authorization config, with two modules assigning
  the same value opposite meanings, but no reachable trigger and no exposed deployment.

---

## The plan-approval gate is inert wherever the ambient session id is unset — the CLI is one such front door

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  Confirmed the writer count: `grep -rn "set_current_session_id" src/` finds one call site,
  `api/runner.py:187` (paired with the reset at `:591`). `build_langgraph_agent` has three callers
  outside its own module — `api/runner.py:123`, `durable/template_activities.py:399` and
  `cli/chat.py:113` — and the template path sets `"harness_enabled": False` in `step_profile`
  (`template_activities.py:485`), so `gate_applies` is False there and the gate is never attached.
  The CLI is therefore the only path where the gate is attached and the session id is unset. That
  part of the finding is right.

  Then drove the **real compiled graph** through the real middleware chain rather than the
  middleware in isolation (`/tmp/vf2/repro.py`: `build_langgraph_agent(model=ScriptedChatModel(...))`,
  `harness_enabled=true`, `harness_autonomy=plan_only`, ambient identity stamped as the CLI stamps
  it, invoked with `turn_input`/`turn_config("cli")` exactly as `cli.converse` does), and read the
  resulting `ToolMessage`:

      # [A] dev checkout posture, no ambient session id (the CLI's shape)
      harness: True autonomy: plan_only entra_required: False gate_applies: True
        ToolMessage[remember_preference] status='success'
          content="Remembered k='v' for this chemist."          # tool body RAN

      # [B] same, with set_current_session_id("cli") — the front door's shape
      harness: True autonomy: plan_only entra_required: False gate_applies: True
        ToolMessage[None] content='Refused: remember_preference changes stored data or starts
          work, and the plan it is part of has not been approved yet; …'

      # [C] shipped-chart posture (entra_required=true, privileged roles empty), CLI shape
      harness: True autonomy: plan_only entra_required: True gate_applies: True
        ToolMessage[None] content='Refused: cli-admin is not authorized to use
          propose_knowledge_note: it changes stored data, so it requires a privileged role the
          account does not hold'

      # [D] shipped-chart posture, CLI shape, a non-write-gated side-effecting tool
        ToolMessage[remember_preference] status='success'
          content="Remembered k='v' for this chemist."          # tool body RAN

  Checked the CLI's own claim about identity: `grep -rn entra_required src/chemclaw/cli/` returns
  two hits, the module docstring and an unrelated comment in `leak_probe.py`. Nothing in `cli/`
  assigns it.

  Checked reachability of the harness: `harness_enabled: bool = False` in
  `core/config/agent.py`; `.env.example:632` is `false`; `deploy/helm/chemclaw/values.yaml:339-340`
  is `"true"` / `plan_only`. `deploy/Containerfile:101` runs `uv sync --frozen --no-dev`, and
  `pyproject.toml:172-174` declares the `chemclaw` console script, so the binary is in the image.

- **Why**

  The **mechanism is confirmed and I would not argue with it**: A vs B above is the same graph,
  the same middleware chain and the same call, differing only in whether the contextvar is stamped,
  and the gate is a no-op in the CLI shape. `cli/chat.converse` passes the session id to LangGraph
  as `thread_id` and never stamps it, so the one control the harness attaches does not run. The
  `/plan` + `/approve` observation is also correct and is the worst part of it: `_plan_command`
  writes a real `plan_approvals` row and prints "approved <hash>; the session may now execute" in
  front of a gate that never consults it.

  What is overstated is the **consequence**, and it rests on a claim about the code that is simply
  false. The finding writes: *"It is not mitigated by RBAC on that path: the CLI runs
  `entra_required=false` by construction (`cli/chat.py:11-18`), so `authorize_tool` is a no-op
  there and the plan gate is the only control over state-changing tools."* No code sets
  `entra_required` anywhere in `cli/`; the CLI reads the same global `Settings` as everything else,
  and the passage cited at lines 11-18 says the **opposite** of what it is cited for — "**It does
  not bypass authorization.** Under `entra_required` the tool gate and the expensive-trigger gate
  still apply". On the posture the chart actually ships, measurement [C] shows
  `enforce_tool_authz` refusing `propose_knowledge_note` before the plan gate is even reached.
  That refusal is not incidental: `cli_admin_roles` defaults to `[]`
  (`core/config/agent.py:126`), and `authorize_tool`'s built-in write gate fails closed on an empty
  privileged set — so in **every** `entra_required` deployment, however `entra_privileged_roles` is
  filled in, the CLI admin holds no role and all four `DEFAULT_WRITE_TOOL_GATES` members are
  refused. The two tools the finding names as the consequence, `propose_knowledge_note` and
  `record_confirmed_answer` — "which push a branch to the knowledge repo" — are precisely the two
  that cannot run on this path. The knowledge-repo write in the headline does not happen.

  What genuinely gets through is measurement [D]: the side-effecting tools that are *not*
  `DEFAULT_WRITE_TOOL_GATES` members — `remember_preference`, `forget_preference`, `watch_for`,
  `stop_watching`, `request_development_report`, plus connector endpoint writes and non-expensive
  job launchers — run unapproved from the CLI under the shipped chart. Real, worth fixing, and
  a materially smaller blast radius than "every state-changing tool runs unapproved".

  Reachability also caps this below high. The gate is only attached when `harness_enabled` is on,
  which is false in the code default and in `.env.example` and true only in the Helm chart — so the
  exposed configuration is the cluster, and the way to reach the CLI there is `oc exec` into a
  chemclaw pod. Whoever can do that already has the pod's service account, its database credential
  and its filesystem; the plan gate is not what stands between them and a `user_preferences` row.
  On a dev checkout the harness has to be turned on deliberately, and the person at the terminal is
  the same person the approval ritual would have asked. The production ingress — the HTTP front
  door, the one place a chemist's turn actually comes from — stamps the session id and refuses
  correctly (measurement [B]), so no user-facing path is affected.

  Nothing here touches a SAFETY or impurity-limit answer: the tools that escape the gate write
  preferences, subscriptions and report requests, and the hazard screen is a read-only MCP tool
  that `gated_call` does not govern in either shape. No chemist is shown a different answer because
  of this defect; what they lose is the approval step in front of a preference or subscription
  write.

  Medium: a real hole in a real gate, reachable only from a shell inside the cluster or a
  deliberately-configured dev checkout, whose named consequence is independently closed by the
  authorization gate the finding wrongly claims is off. The fix the finding proposes
  (`set_current_session_id` around `agent.ainvoke` in `cli.converse`, one line and its reset,
  mirroring `api/runner.py:187/591`) is correct and cheap, and the second half — making the
  "no session" branch fail closed under `gate_applies` rather than skip — is the part that stops
  the next session-less caller from re-opening it.
