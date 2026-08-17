# agent governance — security: reproduction verdicts

Lens: **does it actually reproduce?** Only the two `high` findings are in scope; the three
`medium`/`low` findings in the source file were not examined.

All work done against the shared checkout at `1c60950e` (`git status` clean apart from two other
agents' verdict files; no `MUTANT` marker present in any file I read — `authz.py`, `plan_gate.py`,
`cli/chat.py` and `core/config/entra.py` all matched `git show HEAD:`). My scripts are under
`/tmp/v/`; I did not run the reporter's `/tmp/t*.py`.

---

## An operator gate spelled with an empty role list opens the tool to everyone, including under the `deny` allowlist

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  Re-derived the branch from source. `src/chemclaw/agent/authz.py:329-336` is real and current:

      329:     required = settings.tool_role_gates.get(tool)
      330:     if required is not None:
      331:         if not _has_required_role(frozenset(required)):
      332:             raise AuthorizationError(...)
      336:         return

  and `_has_required_role` at `:283-292` does `if not required: return True`. So an empty list is
  `is not None`, satisfies the predicate, and returns before the `deny` default at `:337`.

  Ran my own script (`/tmp/v/f1.py`), independent of the reporter's:

      compute_dft_energy: ALLOWED
      gather_evidence: refused (attacker-oid is not authorized to use gather_evidence: the account holds none of the roles this tool requires)
      propose_knowledge_note: refused (attacker-oid is not authorized to use propose_knowledge_note: this deployment permits only an approved list of tools, and this one is not on it)
      is compute_dft_energy a DEFAULT_WRITE_TOOL_GATE? True

  Identical to the reporter's transcript. The mechanism is exactly as described.

  I then went further than the finding did, on the **shipped** default (`tool_authz_default="allow"`,
  a privileged role configured):

      gates={}                                     -> refused (…requires a privileged role the account does not hold)
      gates={'propose_knowledge_note': []}         -> ALLOWED
      gates={'propose_knowledge_note': ['']}       -> refused (…holds none of the roles this tool requires)

  i.e. `[]` also overrides the built-in `DEFAULT_WRITE_TOOL_GATES` closure on the default policy —
  a strictly more plausible operator scenario than the reporter's `deny` one, and the reporter
  missed it.

  I also checked whether `[]` can arise other than by an operator typing it, by driving
  `EntraSettings` directly:

      {"t": []}        -> {'t': []}
      {"t": ""}        -> REJECTED: ValidationError
      {"t": null}      -> REJECTED: ValidationError
      {"t": "chemist"} -> REJECTED: ValidationError
      {"t": [""]}      -> {'t': ['']}          # non-empty → fails CLOSED

  and grepped `deploy/`, `infra/`, `examples/` for `tool_role_gates` / `CHEMCLAW_TOOL_ROLE_GATES`:
  **zero** hits. Nothing the project ships writes this key at all.

- **Why**

  The code does what the finding says, on the arguments it says, and the line numbers and symbols
  are current. What the finding gets wrong is that it presents this as an *accident* and offers the
  intra-module asymmetry as its evidence. The asymmetry is a decision the repo made explicitly and
  pinned, and the finding never mentions it —
  `tests/test_authz.py::test_an_operators_empty_role_list_opens_a_tool_rather_than_closing_it`
  (`tests/test_authz.py:227-256`) sets up the reporter's *exact* configuration
  (`entra_required=True`, `tool_authz_default="deny"`, `{"find_notes": []}`), asserts `authorize_tool`
  allows it, and its docstring states the rule and the reason:

      "`tool_role_gates: {tool: []}` means "no role needed", and that convention is now pinned. …
      The asymmetry is deliberate — an operator who writes `[]` against a tool has said something,
      whereas an unfilled chart default has not"

  So this is a dispute about a specified semantic, not a discovered fail-open. That is a real thing
  to argue with, but it is not "the gate silently fails open".

  Three further things cap it below `high`:

  1. **There is no attacker-controlled trigger.** The whole finding is conditioned on an operator
     authoring `[]`. Every near-miss spelling I could construct either rejects at startup or fails
     closed (`[""]` refuses). No chart, env file or example in the repo sets the key.
  2. **The finding's chosen scenario is the least plausible one.** Under `deny`, the way an operator
     closes a tool is to *not list it* — listing is the act of opening. Writing `{"tool": []}` under
     an allowlist to mean "nobody" is a self-contradiction. (The `allow`-default variant I measured
     above is the plausible one, and the finding does not report it.)
  3. Sibling divergence from `skill_access._permits` is real but is a visibility gate, not an
     authorization gate, so "the same config idea has two opposite meanings" overstates the coupling.

  Mechanism: real, reproduces verbatim. Framing and severity: overstated. The proposed fix (treat an
  empty explicit gate as "nobody", plus a startup validator) is still the right change — but it is a
  **deliberate convention change** that must delete or invert that pinned test, and the ticket should
  say so, because the current wording would send someone to "fix a bug" and quietly reverse a decision.

---

## The plan-approval gate is inert wherever the ambient session id is unset — the CLI is one such front door

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Confirmed the cited code is current. `src/chemclaw/agent/plan_gate.py:344-349`:

      344:     session_id = get_current_session_id()
              # No session means no plan to approve and no autonomous loop to gate …
      348:     if not session_id:
      349:         return await handler(request)

  Grepped for every writer of the contextvar across `src/`:

      src/chemclaw/api/runner.py:187:    session_token = set_current_session_id(session.session_id)
      src/chemclaw/api/runner.py:591:        reset_current_session_id(session_token)

  — one writer, as claimed. `cli/chat.py:118-136` (`converse`) passes the session id only as
  `turn_config(session_id)`, i.e. LangGraph's `thread_id`, and never stamps the contextvar.

  Then reproduced it **end to end through the real CLI entry point**, not by driving the middleware
  in isolation (`/tmp/v/f2.py`): `settings.harness_enabled=True`, `harness_autonomy="plan_only"`,
  a real `build_langgraph_agent` with a scripted model that emits one
  `remember_preference` tool call (a `STATE_CHANGING_TOOLS` member), invoked via
  `chemclaw.cli.chat.converse`, with the store swapped for a spy that records whether the body ran:

      gate_applies: True
      [A] CLI shape (no ambient session id):
          answer: done
          tool body ran? True [('cli-admin', 'solvent', 'THF')]
      tool remember_preference failed …: remember_preference changes stored data or starts work,
          and the plan it is part of has not been approved yet …
      [B] front-door shape (ambient session id set):
          answer: done
          tool body ran? False []

  Identical outcome to the reporter's, produced by my own driver. The gate refuses in [B] and is
  never consulted in [A].

  Checked the deployed posture is the gating one — `deploy/helm/chemclaw/values.yaml:339-340`:

      CHEMCLAW_HARNESS_ENABLED: "true"
      CHEMCLAW_HARNESS_AUTONOMY: "plan_only"

  Checked the other two `build_langgraph_agent` callers: `agent/langgraph_agent.py:377` (subagent
  spec) and `durable/template_activities.py:399`. The template step runs with the harness off and a
  `step_profile` that strips undeclared side-effecting tools from the surface, so the docstring's
  "not a hole" claim holds *there*. It does not hold for the CLI, which is a `[project.scripts]`
  console entry point (`pyproject.toml:174`, `chemclaw = "chemclaw.cli.chat:main"`).

- **Why**

  It reproduces exactly, on a shipped front door, under the configuration the shipped chart sets,
  and the tool body executed. `enforce_plan_approval` is the whole of D-167's control and it is a
  no-op on that path; meanwhile `cli/chat.py:240-289` writes real `plan_approvals` rows and prints
  `"approved <hash>; the session may now execute"`, so the CLI presents an approval ritual in front
  of a gate that cannot fire. That combination — an inert control plus a UI that reports it as
  active — is the worst version of this class, and I would keep the reporter's `high`.

  One supporting claim in the finding is **wrong**, and the triage should not carry it forward:
  "the CLI runs `entra_required=false` by construction (`cli/chat.py:11-18`)". `grep -rn entra_required
  src/chemclaw/cli/` finds no assignment — the CLI never sets it, and `resolve_identity`
  (`cli/chat.py:65-88`) stamps a real ambient identity precisely so `authorize_tool` and
  `authorize_trigger` keep applying; the module docstring's "It does not bypass authorization" is
  true. So "the plan gate is the *only* control over state-changing tools" on that path holds only
  in a dev checkout where `entra_required` defaults to `False`, not "by construction". Under a real
  deployment's env the RBAC gate is still in front of the four `DEFAULT_WRITE_TOOL_GATES` names.
  That narrows the claim; it does not touch the defect, because the plan gate governs 20+ tools that
  RBAC leaves open under the shipped `allow` default.

  Two smaller inaccuracies, neither load-bearing: `get_current_session_id()` returns `None`, not
  `""` (`core/session_context.py:36`); and reaching this requires `--admin` plus a shell in the
  checkout, so the principal is a trusted operator — the property lost is the human-in-the-loop
  check on what the *model* does, not an escalation for an untrusted user. The knowledge-graph
  PR-gate also still stands behind `propose_knowledge_note`, so an unapproved call pushes a branch
  rather than merging one. The reporter's one-line fix (stamp/reset the session id around
  `agent.ainvoke` in `converse`) is correct and I verified it is sufficient: case [B] above is that
  fix, and it refuses.
