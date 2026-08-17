# Verification — `agent/` graph slice, design & simplification lens

Scope: only the one **high** finding in
`tasks/audit-2026-08-16/findings/round1/agent-graph--design.md`. The other seven are medium/low and
were not verified.

I did not run the reporter's `/tmp/refcheck*.py`, `/tmp/prose_ratio.py`, `/tmp/repro_drift.py` or
`/tmp/swap2.py`. Every number below comes from a script I wrote (`/tmp/myres.py`, `/tmp/pr.py`,
`/tmp/scan.py`) or a command I ran myself.

---

## The explanatory prose has decayed: eight symbol references in this slice resolve to nothing

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  **1. Resolved every named symbol against the tree, with my own AST index.** `/tmp/myres.py` walks
  every `.py` under `src/` and collects two sets: `def`/`class` names only, and *any* name binding
  (assignments, parameters, attribute accesses, keywords). A symbol that fails even the permissive
  set exists nowhere in the package.

  ```
  == defined-anywhere-in-src (any binding kind) ==
    connector_tools          name-bound:True  module:False
    _narrow_allowed_tools    name-bound:False module:False
    skills_source            name-bound:False module:False
    _build_skills            name-bound:False module:False
    _anthropic_client        name-bound:False module:False
    challenge_gate           name-bound:False module:False
    usage_tokens             name-bound:False module:False
    chat_client              name-bound:False module:False
    challenge_answer         name-bound:False module:False
    _default_client          name-bound:True  module:False
    after_model              name-bound:False module:False

  == def/class only ==
    connector_tools          def/class:False
    ... (all of the above False except _default_client: True)
  ```

  `connector_tools`'s single `name-bound:True` is a **local variable** in
  `durable/template_activities.py:263` (`connector_tools, unreachable = await open_connector_specs(...)`),
  not a definition. `_default_client` resolves — but only the `agent/verifier._default_client` half
  of `llm_provider.py:329`; `agent/challenge` is not a module (`ls src/chemclaw/agent/challenge*` →
  no such file).

  **2. Checked every cited line number holds the cited text.** All thirteen do, verbatim:

  ```
  chemclaw_agent.py:226  Computed from the manifests rather than by calling `connector_tools`, deliberately: building a
  chemclaw_agent.py:230  `_capability_tools` and `connector_tools` really produce, so the two narrowings cannot drift.
  chemclaw_agent.py:243  The MCP half mirrors `connector_tools` exactly — `mcp_server_names` selects whole bundles, then
  chemclaw_agent.py:304  never widening. It spans **both** halves: the in-process tools here and, in `connector_tools`,
  chemclaw_agent.py:443  Identical policy to `connector_tools`, over the other engine's connector representation: both
  chemclaw_agent.py:473  `dataclasses.replace` rather than the in-place mutation `_narrow_allowed_tools` uses: a
  langgraph_agent.py:135      `build_langgraph_agent(chat_client=...)` is: the wiring must be assemblable and testable
  langgraph_agent.py:143  precedence and same reason as `build_langgraph_agent`: an agent outlives a turn, so
  langgraph_agent.py:152  the same promise `build_langgraph_agent` makes.
  langgraph_agent.py:178  A compiled graph. No network call happens here; construction only, exactly as
  langgraph_agent.py:189  pinned to its connectors' — one turn. `chemclaw_agent.connector_tools` records the rule; this
  langgraph_agent.py:460  Private, and split from `skills_backend` for the reason `chemclaw_agent.skills_source` is split
  langgraph_agent.py:461  from `_build_skills`: the backend is the part with behaviour and the middleware is somebody
  langgraph_agent.py:490  The LangGraph twin of `chemclaw_agent.skills_source`, narrowed by the *same* three predicates
  loop_cap.py:106        reverse list order, so `challenge_gate.challenge_answer` — `@after_model(can_jump_to=[...])`
  llm_provider.py:236    failure `usage_tokens`'s own docstring records — 50 turns of 15,000 real tokens booked as zero
  llm_provider.py:270    The preflight is kept for the reason `_anthropic_client` gives: a missing key should fail here,
  llm_provider.py:329    the cost `agent/verifier._default_client` and `agent/challenge._default_client` already pay
  ```

  **3. Confirmed the self-reference claim by AST, not by eye.** An enclosing-function lookup for the
  cited lines prints:

  ```
  135 -> build_langgraph_agent (def at 118)
  143 -> build_langgraph_agent (def at 118)
  152 -> build_langgraph_agent (def at 118)
  178 -> build_langgraph_agent (def at 118)
  189 -> build_langgraph_agent (def at 118)
  ```

  So `build_langgraph_agent`'s own docstring says its `checkpointer` argument is supplied by the
  caller because "`build_langgraph_agent` is synchronous and resource-free by the same promise
  `build_langgraph_agent` makes" — a sentence comparing the function to itself. Same for `:143`
  ("same reason as `build_langgraph_agent`") and `:178` ("exactly as `build_langgraph_agent`
  promises"). `git log -S"def build_agent" -- src` shows the rename landed in `25fa3255`; `git log
  -S"def connector_tools"`, `-S"def usage_tokens"` and `--diff-filter=D -- agent/challenge_gate.py`
  point at `25fa3255`, `e453c201` and `6b6662da` respectively — the symbols were real and were
  deleted without their prose.

  **4. Re-measured the prose ratio with my own tokenizer+AST script** (`/tmp/pr.py`, docstring spans
  from `ast`, comments from `tokenize`, blanks excluded):

  ```
  file                         total  code  prose  prose%
  langgraph_agent.py             646   180    391     68%
  chemclaw_agent.py              526   264    196     43%
  checkpointer.py                406   114    225     66%
  compaction.py                  354   103    202     66%
  loop_cap.py                    207    32    136     81%
  graph_tools.py                 352   162    145     47%
  llm_provider.py                345    78    201     72%
  TOTAL                                933  1496     62%
  ```

  Identical to the reporter's table except `compaction.py` (103/202 vs 101/204 — a two-line
  comment-vs-docstring boundary). 62% reproduces exactly.

  **5. Confirmed nothing checks these.** `tests/test_docstring_paths.py` guards backticked *paths*
  ending in `.py` and *fully-qualified* `chemclaw.a.b.c` names (`_QUALIFIED = re.compile(r"`(chemclaw(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")`).
  A bare `` `connector_tools` `` or a two-segment `` `chemclaw_agent.skills_source` `` falls outside
  both. `uv run pytest tests/test_docstring_paths.py tests/test_prose_contract.py -q` → **625
  passed** with all thirteen references dangling.

  **6. Checked the finding is not padded.** `/tmp/scan.py` resolves every backticked dotted/private
  identifier in the seven slice files against the same index. The only unresolved first-party names
  it finds are exactly the reporter's: `_anthropic_client`, `_build_skills`, `_narrow_allowed_tools`,
  `challenge_gate.challenge_answer`, `chemclaw_agent.skills_source`, plus the one the reporter
  correctly excludes as deliberate (`compaction.py:5`'s `chemclaw_agent._build_compaction`). The
  other hits — `_apply_custom_middleware`, `_should_summarize`, `_build_task_tool`,
  `_apply_excluded_middleware` — all resolve into `deepagents/` in the venv, so they are valid.

  **7. Tested the strongest "load-bearing" claim by execution.** `chemclaw_agent.py:226-229` gives
  the reason for not calling the builder as "building a connector's MCP tool opens an
  `httpx.AsyncClient` that only a turn's exit stack ever closes". I counted live httpx clients
  across `connector_specs()`:

  ```
  specs: [('bo', (...5 tools)), ('calc', (...15)), ('chem', (...4)), ('molfp', (...2)),
          ('rxnfp', ('similar_reactions',)), ('safety', (...3))]
  httpx clients before/after connector_specs(): 0 0
  ```

  Zero clients. The stated justification for the duplicated narrowing is not merely a dangling name,
  it is **false about the current code**.

  **8. Tested the weakest claim — that pillar #1 of `loop_cap.py` is unverifiable.**
  `grep -rn "after_model" src` returns only prose; my AST scan confirms no `after_model` hook is
  defined anywhere in `src/`, so the example really is gone. But the *general* rule is stated
  independently 25 lines below at `loop_cap.py:129-132` ("**`ModelCallLimitMiddleware` is unsafe to
  compose with any middleware that jumps from `after_model`.**"), and I verified its premise against
  the installed upstream rather than the prose:

  ```
  langchain/agents/middleware/model_call_limit.py
    def after_model  -> True   (the increment)
    def before_model -> True   (the check)
    ModelCallLimitState.thread_model_call_count: Annotated[int, PrivateStateAttr]
    ModelCallLimitState.run_model_call_count:    Annotated[int, UntrackedValue, PrivateStateAttr]
  ```

  Pillars 1, 3 and 4 still hold against the shipped dependency. A maintainer *can* check the
  constraint; what they cannot check is the worked example.

- **Why**

  Every factual claim in this finding reproduces, independently derived: thirteen line numbers exact,
  eight first-party symbols resolving to nothing under a deliberately permissive index, four
  self-referential sentences confirmed by enclosing-function lookup, the 62% prose ratio reproduced
  to the point, no existing guard covering the shape, and the strongest "load-bearing" instance
  measured false (0 httpx clients). The finding is also *not* padded — an independent scan of the
  slice finds no dangling first-party name the reporter missed and no upstream name miscounted as
  one.

  Two things pull it below **high**, which is why the verdict is OVERSTATED rather than CONFIRMED.

  First, item 2's stated consequence — "a maintainer … cannot tell whether the constraint still
  holds" — does not survive. The same docstring states the rule in general form and its premise
  checks out against the installed `langchain` in one `inspect` call. The dead `challenge_gate`
  example costs a reader a `grep`, not the argument.

  Second, and decisive for severity: the finding is comments-only with no runtime consequence
  anywhere. Its worst outcome is a declined refactor — a reader who believes the httpx sentence
  leaves the divergent `_advertised_names` mirror alone. That is a real cost and it is the reporter's
  own *medium* finding. A defect whose entire consequence is "you must read one more file to trust a
  comment" is a medium in a tree where every gate stays green.

  Two things the reporter missed, both making it worse and neither changing the verdict:

  - **The decay is repo-wide, not slice-wide.** `grep -rn '`connector_tools`\|`build_agent`\|`skills_source`\|`challenge_gate' src tests --include=*.py`
    returns **22** hits across 13 files. Seven are in the slice. `connectors/registry.py:645-646,651`
    carries the *same* dead `connector_tools` pointer **and the same false httpx justification** for
    `endpoint_tool_names` — the function the divergent mirror is built on. Fixing only the slice
    leaves the load-bearing half of that sentence in place one module over.
  - **The test suite has the same decay, and the finding's proposed check is scoped to `src/` only.**
    `tests/test_profile_discovery.py:145` — the docstring of the very test the code cites as its
    anti-drift pin — reads "`advertised_tool_names` must equal what `build_agent` + `connector_tools`
    actually produce", naming two functions that no longer exist.

  So: the mechanism is real, the evidence is clean, and the fix is right. The severity is not.
