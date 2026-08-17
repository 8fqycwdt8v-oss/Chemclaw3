# Verdicts — sweep-dynamic-resolution, reachability lens

Scope: critical/high findings only. The file contains exactly **one** high finding (#1); #2, #3, #4
and #7 are medium, #5 and #6 are low. Those six were not examined.

Working tree check: `src/chemclaw/agent/profiles.py` and `src/chemclaw/agent/plan_gate.py` are
byte-identical to the pristine `HEAD` copy at
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/pristine`, so no
other agent's mutation is in play here.

---

## 1. A profile file's `harness_autonomy` is an unvalidated string, so a typo silently turns the plan gate off

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (agrees with the report; see the caveat at the end)

### What I did

**Reproduced the mechanism end to end.** Four profile files in `/tmp/prof1`, loaded through the real
discovery path with `CHEMCLAW_PROFILES_DIR=/tmp/prof1`:

```
$ CHEMCLAW_PROFILES_DIR=/tmp/prof1 uv run python -c "...load_profiles(); print gate_applies..."
default    enabled=False  autonomy='plan_only'    gate_applies=False
honest     enabled=True   autonomy='plan_only'    gate_applies=True
implicit   enabled=True   autonomy='plan_only'    gate_applies=True     # field omitted → global wins
typo       enabled=True   autonomy='plan-only'    gate_applies=False    # ← the defect
```

`implicit.yaml` is the control the report did not include and the one that settles the direction of
harm: with the field **absent** the gate applies (the global default `plan_only` is inherited); with
the field present-but-misspelled the gate is **off**. A line an author added to be explicit about
wanting the gate is what removes it.

**Confirmed the misspelled-*key* half of the docstring is true and the misspelled-*value* half is
not.** With a fifth file `badkey.yaml` (`harness_autonmy: plan_only`) in the same directory,
`load_profiles()` raised:

```
ProfileError /tmp/prof1/badkey.yaml: invalid profile: 1 validation error for AgentProfile
harness_autonmy
  Extra inputs are not permitted [type=extra_forbidden, ...]
```

So `extra="forbid"` (`profiles.py:48`) catches the key and nothing catches the value. The class
docstring at `profiles.py:43-45` claims fail-fast "the same fail-fast the config models use" — for
this field that claim is false, and the finding is right to call it out.

**Confirmed the asymmetry with the env-var path is real** (the finding asserts it; I ran it):

```
$ CHEMCLAW_HARNESS_AUTONOMY=plan-only uv run python -c "from chemclaw.core.config import settings"
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
harness_autonomy
  Input should be 'plan_only' or 'execute' [type=literal_error, input_value='plan-only', ...]
```

Hard startup failure via env var (`core/config/agent.py:142` is a `Literal`); silence via file.

**Traced every candidate upstream guard and found none.**

- `grep -rn "harness_autonomy" src/` → the only readers are `plan_gate.autonomy_for:159` and the
  `Literal` on the settings field. No second consumer that would raise on an unknown value.
- `grep -rn "autonomy_for\|gate_applies\|harness_enabled_for" src/` → three call sites:
  `langgraph_agent.py:452` (harness middleware), `langgraph_agent.py:630`
  (`*([enforce_plan_approval] if gate_applies(profile) else [])`), `api/runner.py:166`
  (`plan_gated = gate_applies(...)`). All three take the boolean and branch; none validates the
  string.
- `Makefile` `.PHONY` list and the `ci:` target — eight validators (`kg-`, `eln-`, `skill-`,
  `connector-`, `datasource-`, `template-`, `prose-`, `helm-`), **no `profile-validate`**. The
  finding's claim that no CI stage sees it holds. `template-validate` does resolve profile *names*,
  but the name is the filename and the value is never inspected.
- `profile_discovery._load` (`:55-71`) logs nothing about the value; `load_profiles` logs only
  `"registered N file profile(s)"` with the names. `langgraph_agent._harness_middleware:452` and the
  chain builder at `:630` log nothing either. "Nothing logs, nothing raises" is verified, not
  assumed.

**Checked the deployment shape, because that is what decides reachability.**
`deploy/helm/chemclaw/values.yaml:339-340` sets `CHEMCLAW_HARNESS_ENABLED: "true"` and
`CHEMCLAW_HARNESS_AUTONOMY: "plan_only"`. So the harness-on, gate-on posture is not a hypothetical
opt-in — it is the shipped chart's default. `deploy/Containerfile` sets `WORKDIR /app` and
`COPY data ./data`, so `data/profiles/*.yaml` is the intended authoring surface in that image, and
all six shipped profiles live there. The precondition the finding names (`harness_enabled=true`) is
satisfied by the chart, not by an unusual operator choice.

### Why

Every factual claim in the finding holds under execution, and I could not find anything upstream
that stands in the way: no `Literal`, no pydantic validator, no startup guard, no Makefile target,
no CI stage, no log line, and no second reader that would trip over the bad value. The trigger is
producible by the ordinary authoring path for the shipped image, under the shipped chart's own
settings.

Two things I would add that the reporter missed, both of which cut toward keeping the severity
rather than lowering it:

1. **The control-plane display disagrees with enforcement, which is the exact failure the module
   was written to prevent.** With the typo profile, `harness_enabled_for` is still `True`, so
   `TodoListMiddleware` is attached (`langgraph_agent.py:452`) and the session still proposes a
   plan. `GET /sessions/{id}/plan` (`api/routes/plan.py:get_plan`) then reports
   `approved=false, decided_by=None` — because nobody has decided — **while every state-changing
   call executes**. That module's own docstring calls this shape out by name ("a route reporting a
   plan is approved while every state-changing call under it is refused … the same disagreement …
   that let DARK-1 sit unnoticed"); the typo produces the inverse, which is the dangerous polarity
   of the same disagreement. A surface built to show "awaiting approval" shows exactly that while
   the work proceeds.
2. **The fail-open direction is asymmetric in the worst way.** A typo on the `execute` side
   (`harness_autonomy: exectue`) leaves `gate_applies` false — same as intended. A typo on the
   `plan_only` side removes a control the author was asking for. Only one of the two possible typos
   has a consequence, and it is the safety-relevant one.

What does **not** hold up, and why it does not change the verdict: the loss is one layer, not the
last one. `enforce_tool_authz` still runs, and `DEFAULT_WRITE_TOOL_GATES` (`agent/authz.py:81-88`)
closes `compute_dft_energy`, `propose_knowledge_note`, `record_confirmed_answer` and `record_failure`
to the privileged role set by default; knowledge-graph writes are still proposals a human merges;
the audit trail and `refuse_writes_on_dry_run` are untouched. What is genuinely lost without the gate
is the *pre-execution* approval over durable job launches, `state_changing` connector tools,
preference/subscription writes and `/memories/` writes. The finding says exactly that and does not
overstate it.

**On safety answers specifically** (the lens's standing question): this defect changes nothing a
chemist is shown for a hazard or ICH Q3C/Q3D impurity-limit query. All of `screen_hazards`,
`screen_genotoxic_alerts` and `ich_impurity_limit` are read-only, so `side_effecting_call` is false
for them and `enforce_plan_approval` never governs them whether it is attached or not. Losing the
gate neither suppresses nor alters a safety answer; it only removes the approval in front of writes
and job launches.

**The one thing I would flag about the report's own scale**, without letting it lower this verdict:
finding 2 (rated *medium*) describes the identical consequence — a profile file removing the plan
gate — reached by a *broader* trigger (any authenticated caller selecting a widening profile). The
two cannot be a full notch apart. I would raise #2 rather than lower #1, because #1's version is
silent and #2's is at least intentional, but the inconsistency is worth the reporter's attention.

The proposed fix (`harness_autonomy: Literal["plan_only", "execute"] | None = None`, importing the
literal from one place) is correct and is genuinely one line; it converts this into a `ProfileError`
at startup naming the file, matching the env-var path.
