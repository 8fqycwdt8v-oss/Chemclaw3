# Verdicts — `agent/` graph slice, design & simplification (reachability lens)

Scope: findings marked **critical** or **high**. That is exactly one finding in this file; the
other seven are medium/low and were not verified.

---

## The explanatory prose has decayed: eight symbol references in this slice resolve to nothing

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

**1. Resolved all eight symbols independently.** Definition search over `src/` *and* `tests/`:

```
$ for s in connector_tools _narrow_allowed_tools skills_source _build_skills \
           _anthropic_client challenge_answer usage_tokens _default_client; do
    grep -rnE "^\s*(async )?def $s\b|^\s*class $s\b|^\s*$s\s*=" src tests --include=*.py; done

connector_tools          (nothing)
_narrow_allowed_tools    (nothing)
skills_source            (nothing)
_build_skills            (nothing)
_anthropic_client        (nothing)
challenge_answer         (nothing)
usage_tokens             (nothing)
_default_client          src/chemclaw/agent/verifier.py:281:def _default_client() -> Any:
```

Also confirmed the sub-claims: there is no `challenge*` module under `src/` (`find src -name
'challenge*'` → empty), and `grep -rn after_model src --include=*.py` returns **only prose** —
`loop_cap.py:105,106,131`, `langgraph_agent.py:450`, `state.py:93-94`, `core/config/agent.py:166`.
No `@after_model` hook exists. The four self-referential sentences at `langgraph_agent.py:135,143,
152,178` read as described, and `build_langgraph_agent`'s signature (`:118-130`) has `model:`, not
`chat_client=`.

**2. Confirmed no gate covers this corpus.** `make prose-validate` is green:

```
$ make prose-validate
prose contract OK: every named tool, note type, path, ADR id, config key and metric resolves
EXIT=0
```

Its corpus is `_prose_sources()` (`_INSTRUCTIONS` + every `SKILL.md`) and `_operator_sources()`
(`_OPERATOR_DOCS` + `docs/guides/*.md`, `docs/reference/*.md`, `Makefile`, `.env.example`) —
`src/**/*.py` docstrings are not in any of the three corpora.

**3. Found the gate the reporter missed.** `tests/test_docstring_paths.py` is exactly this class of
guard and is live and green:

```
$ uv run pytest tests/test_docstring_paths.py -q
592 passed in 1.34s
```

Its module docstring names the very case the reporter is generalising from — *"`build_agent` had
**zero definitions** and 32 references in source docstrings"* — and the comment beside `_QUALIFIED`
(`tests/test_docstring_paths.py:89-103`) states that the two wider rules were tried against the tree
and rejected on measured false-positive rate.

**4. Re-measured that rejection myself rather than taking the comment's word** (`/tmp/vx/fp.py`,
regex-matching every backticked span in `src/` + `tests/` against every name AST-defined in `src/`):

```
rule A (bare backticked identifier): 3429 distinct, 1503 unresolved = 44% FP
  sample unresolved: ['A','AIMessage','AIMessageChunk','ALTER','AND','ANALYZE','ANY','APIRouter',...]
rule B (`module.symbol`):            1173 occurrences, 648 unresolved = 55%
  sample unresolved: ['science.calc','pd.Series','connector.yaml','json.dumps','hnsw.ef_search',
                      'tempfile.TemporaryDirectory','xtbopt.xyz','calc.xtb_hessian',...]
```

My cruder patterns give higher rates than the in-tree figures (32% / 426-of-528), but the conclusion
is the same and it is a *measurement*, not the comment's claim.

**5. Checked whether any of the eight is load-bearing.** `grep -rn "__doc__" src --include=*.py`
returns 13 hits, all in `argparse` descriptions and the two generated-launcher builders
(`templates/registry.py:135,216`, `connectors/jobs.py:192,451`). None of the seven slice modules is
in that set, and none of the flagged docstrings belongs to a `@tool` function or feeds a prompt.
Nothing at runtime reads any of these strings.

**6. Checked whether the loop_cap constraint is actually unrecoverable.** It is not. The same
docstring states the rule in general form, independent of the dangling example
(`loop_cap.py:130-132`: *"`ModelCallLimitMiddleware` is unsafe to compose with any middleware that
jumps from `after_model`"*), it is restated at `state.py:93-94`, `core/config/agent.py:166`,
`tests/test_langgraph_stream.py:282` and `tests/test_langgraph_agent.py:776`, and the upstream
property it depends on is machine-pinned:

```
tests/test_upstream_surface.py:143
  assert "UntrackedValue" in annotation and "OmitFromSchema" in annotation, (
      "ModelCallLimitMiddleware's run counter is now readable from a finished run; ...")
```

**7. Checked the two weakest of the eight.** `llm_provider.py:236`'s `usage_tokens` — the correct
name appears **twice in the same docstring**, five lines above (`:172` and `:231`,
`runner_usage.graph_usage_tokens`), and the content it cites is verifiably there:
`api/runner_usage.py:112` (inside `graph_usage_tokens`, which starts at `:91`) contains *"50 turns
of 15,000 real tokens each were booked as…"*. `llm_provider.py:329`'s `agent/challenge._default_client`
is cited in the same sentence as `agent/verifier._default_client`, which exists at `verifier.py:281`
with exactly the `@cache` rationale the sentence borrows.

### Why

**Reachability: granted, in full.** The trigger is "read the file", there is no gate in the way, and
I proved the absence of one by running the gate that exists and finding this corpus outside all three
of its source sets. The mechanism reproduces exactly as filed: all eight names have zero definitions
anywhere in `src/` or `tests/`. Nothing in this verdict disputes that.

**Consequence: three of the claims that carry the `high` do not hold.**

*"It is not checked by anything"* — false as a statement about the class. `tests/test_docstring_paths.py`
(592 parametrized cases) is precisely this guard, written after the `build_agent` deletion the
reporter is describing the aftermath of. The eight survive it because they are written in the two
shapes whose scope was **measured and deliberately excluded**, not because nobody thought of it. That
matters because the finding's own remediation — *"every backticked `module.symbol` or `` `_private_symbol` ``
must resolve … with an explicit allow-list"* — is that rejected rule, re-proposed without knowing it
had been tried. My independent measurement (44% / 55% unresolved, firing on `json.dumps`,
`pd.Series`, `AIMessage`, `connector.yaml`, `ALTER`) says the "explicit allow-list" would need
several hundred entries on day one. Acting on the fix as filed would make the build red for a
corpus the tree already decided it cannot resolve.

*"Several are load-bearing"* — none is, in the sense this audit defines (named in a docstring a
caller relies on). No runtime reader touches any of these strings; the finding itself closes with
*"Behaviour-preserving: yes, entirely (comments only)"*. The one site offered as load-bearing —
`chemclaw_agent.py:243`'s mirror claim, and with it `:230`'s *"so the two narrowings cannot drift"* —
is load-bearing only if the mirror is in fact broken, which is the reporter's **own next finding,
self-rated medium**. A `high` cannot be assembled by importing the severity of a `medium` filed
three paragraphs later.

*"A maintainer … cannot tell whether the constraint still holds"* — false for the case chosen to
carry it. `loop_cap.py` states the general rule six lines below the dangling example, three other
modules restate it, and `tests/test_upstream_surface.py:143` fails the build the day upstream's
property changes. The dangling `challenge_gate.challenge_answer` costs a reader the *illustration*,
not the argument.

The 62%-prose measurement is real (I re-derived `loop_cap.py` at 168 non-blank / 136 prose / 32 code
and got the reporter's numbers exactly) but it measures the slice, not the harm. A ratio is not a
consequence.

**What I would add that makes it slightly worse than the reporter had it.** The decay is not confined
to the slice, and not confined to `src/`: `connectors/registry.py:651`, `tests/test_tool_registry.py:82`,
`tests/test_profiles.py:70,141,164-165`, `tests/test_skill_access.py:8`, `tests/test_turn_observability.py:6`,
`tests/test_authz.py:197` and `tests/test_profile_discovery.py:144-146` all name `connector_tools` or
`build_agent`. `test_profile_discovery.py`'s docstring is the sharpest of these — it explains what the
test does in terms of two functions that do not exist. Inside the slice, `loop_cap.py:117-123` has a
second stale pillar the reporter did not flag: regressions #2 and #3 argue from "a capped specialist
reported the limit string" and "a five-specialist turn's ceiling went from ~N to ~6N", and there are
no specialists (`grep -rn specialist src` finds prose only; `agent/subagents.py` is the one-name
helper roster). So the loop_cap passage is stale in three of its four pillars, not one.

**Net.** Real, cheap, worth fixing as ~10 line edits plus the out-of-slice sites; not worth the gate
proposed with it. No runtime path, no user-visible effect, no correctness or security consequence,
and the one design argument said to be unrecoverable is stated generally in the same docstring and
pinned by a live test. That is **low**. If one insists on counting `:230`'s false "cannot drift"
assertion as its own harm, `medium` is the ceiling — and that harm is already booked under the next
finding.
