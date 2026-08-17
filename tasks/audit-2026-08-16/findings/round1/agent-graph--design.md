# Round 1 — `agent/` graph slice, design & simplification lens

Slice: `src/chemclaw/agent/{langgraph_agent,chemclaw_agent,checkpointer,compaction,loop_cap,graph_tools,llm_provider}.py`

All measurements below were produced by running scripts under `/tmp` against the live venv
(`uv run`). Script output is quoted verbatim.

---

## The explanatory prose has decayed: eight symbol references in this slice resolve to nothing

- **Severity**: high
- **Location**:
  - `src/chemclaw/agent/chemclaw_agent.py:226,230,243,304,443` (`connector_tools`), `:473` (`_narrow_allowed_tools`)
  - `src/chemclaw/agent/langgraph_agent.py:189` (`chemclaw_agent.connector_tools`), `:460,490` (`chemclaw_agent.skills_source`), `:461` (`_build_skills`)
  - `src/chemclaw/agent/loop_cap.py:106` (`challenge_gate.challenge_answer`)
  - `src/chemclaw/agent/llm_provider.py:270` (`_anthropic_client`), `:329` (`agent/challenge._default_client`), `:236` (`usage_tokens`)
  - `src/chemclaw/agent/langgraph_agent.py:135,143,152,178` (self-referential sentences)
- **Trigger**: open any of these files and follow a `` `backticked` `` pointer the prose gives as the
  reason for a design choice.
- **Consequence**: the prose is 62% of this slice by line count and is the only place the design
  decisions are recorded, but it is not checked by anything, and it has already drifted. Three
  distinct failure modes, all present:

  1. **Pointers to deleted symbols.** `connector_tools`, `_narrow_allowed_tools`, `skills_source`,
     `_build_skills`, `_anthropic_client`, `agent/challenge`, `challenge_gate`, `usage_tokens` are
     all absent from `src/`. Several are load-bearing: `chemclaw_agent.py:243` justifies a
     duplicated narrowing rule with *"The MCP half mirrors `connector_tools` exactly … the two
     disagreeing is the only way this can be wrong"* — the function it must mirror does not exist,
     and the mirror is in fact broken (see the next finding).
  2. **A stale reason for a live design choice.** `loop_cap.py:105-111` gives regression #1 —
     the whole "why not `ModelCallLimitMiddleware`" argument's first pillar — as
     *"`challenge_gate.challenge_answer` — `@after_model(can_jump_to=[...])` — runs first, and its
     `jump_to: "model"` short-circuits the rest of the chain including the increment"*, in the
     present tense. There is no `challenge_gate` module and **no `@after_model` hook anywhere in
     `src/`**. A maintainer checking the argument finds a module that isn't there and cannot tell
     whether the constraint still holds.
  3. **Self-reference from a global rename.** A `build_agent` → `build_langgraph_agent` sweep turned
     four comparison sentences inside `build_langgraph_agent`'s own docstring into statements about
     itself. The clearest, `langgraph_agent.py:150-152`:

     > it is an async factory that migrates on first use, and `build_langgraph_agent` is
     > synchronous and resource-free by the same promise `build_langgraph_agent` makes.

     and `:135`, which cites a keyword argument that does not exist on any function in the repo:

     > Injectable for the same reason `build_langgraph_agent(chat_client=...)` is

- **Evidence**: `/tmp/refcheck.py` and `/tmp/refcheck2.py` index every `def`/`class`/module-constant
  name in `src/`, then check every backticked identifier in the slice against it.

  ```
  --- langgraph_agent.py
    line 189: `chemclaw_agent.connector_tools`  (module exists: True)
    line 460: `chemclaw_agent.skills_source`    (module exists: True)
    line 490: `chemclaw_agent.skills_source`    (module exists: True)
  --- loop_cap.py
    line 106: `challenge_gate.challenge_answer` (module exists: False)
  ...
  connector_tools     x 5  chemclaw_agent.py:226, :230, :243, :304, :443
  _narrow_allowed_tools x 1  chemclaw_agent.py:473
  _build_skills       x 1  langgraph_agent.py:461
  _anthropic_client   x 1  llm_provider.py:270
  ```

  ```
  $ grep -rn "chat_client" src --include=*.py
  src/chemclaw/agent/langgraph_agent.py:135:  `build_langgraph_agent(chat_client=...)` is: ...
  ```

  `/tmp/prose_ratio.py` (AST docstrings + tokenized comments vs executable lines, blanks excluded):

  ```
  file                          total   code  prose  prose%
  langgraph_agent.py              646    180    391     68%
  chemclaw_agent.py               526    264    196     43%
  checkpointer.py                 406    114    225     66%
  compaction.py                   354    101    204     67%
  loop_cap.py                     207     32    136     81%
  graph_tools.py                  352    162    145     47%
  llm_provider.py                 345     78    201     72%
  TOTAL                                  931   1498     62%
  ```

  `loop_cap.py` is 32 lines of code carrying 136 lines of prose, of which the passage that has
  gone stale is the longest.
- **Fix**: two changes, both mechanical.
  1. Add the `/tmp/refcheck.py` check as a test (`tests/test_docstring_references.py`): every
     backticked `module.symbol` or `` `_private_symbol` `` in `src/chemclaw/**` must resolve to a
     name defined in `src/`, with an explicit allow-list for deliberate historical references
     (`compaction.py:5`'s `chemclaw_agent._build_compaction` is one — it is explicitly past-tense
     and correct). This turns the next deletion-without-prose-update into a red build, which is
     the only mechanism that will hold at a 62% prose ratio.
  2. Fix the ten sites above. `loop_cap.py:105-111` needs the strongest edit: state the general
     rule (`after_model` counters are skippable by any `after_model` jumper) and drop the
     `challenge_gate` example, or say plainly that the middleware that demonstrated it has since
     been removed.

  Behaviour-preserving: yes, entirely (comments only, plus one new test).

---

## Two implementations of the connector-narrowing rule, provably divergent

- **Severity**: medium
- **Location**: `src/chemclaw/agent/chemclaw_agent.py:240-250` (`_advertised_names`) vs
  `:440-488` (`connector_specs` + `_narrow_allowed_specs`)
- **Trigger**: any enabled connector whose manifest declares an `endpoint` with no `tools:` key.
  `ConnectorManifest.endpoint.tools` is `Field(default_factory=list)` — empty is valid and passes
  offline `make connector-validate`.
- **Consequence**: the two functions answer the same question ("what does this profile advertise?")
  and give different answers.
  - `registry._mcp_connection:427` writes `allowed_tools=tuple(endpoint.tools) if endpoint.tools else None`,
    i.e. empty → `None` → "everything this server offers".
  - `_narrow_allowed_specs:484` reads that `None` as `sorted(keep)` — the profile's **entire**
    `tool_names` set.
  - `_advertised_names:247` goes through `endpoint_tool_names`, which does
    `names.update(manifest.endpoint.tools)` — contributing **nothing**.

  `advertised_tool_names` is what `skill_access.skill_permits(available=…)` scopes skills against
  and what `cli/validate_templates.py:248` checks step tool references against. So a tool that the
  connector really serves and the agent really binds is invisible to the skill gate (the skill is
  silently hidden) and to template validation (a correct reference fails). That is the exact D-117
  failure mode the module docstring says `available_tool_names` exists to prevent.
- **Evidence**: `/tmp/repro_drift.py` mocks `registry.enabled()` with one such manifest and calls
  the two real functions:

  ```
  connector_specs() built     : [('wide', ('find_notes', 'predict_pka', 'screen_hazards'))]
  _advertised_names mcp half  : []
  MISMATCH: ['find_notes', 'predict_pka', 'screen_hazards'] reachable via the spec, absent from advertised_tool_names

  unnarrowed spec allowed_tools: [('wide', None)] (None = bind whatever the server advertises)
  unnarrowed advertised mcp half: []
  ```

  `tests/test_profile_discovery.py:140` asserts the two agree, but only over the six shipped
  profiles and the shipped manifests, all of which enumerate their tools — so the drift is invisible
  to the suite.
- **Fix**: make `_advertised_names` derive the MCP half from `connector_specs(profile)` rather than
  re-applying the rule:

  ```python
  mcp = {name for spec in connector_specs(profile) for name in (spec.allowed_tools or ())}
  ```

  This is exactly what `tests/test_profile_discovery.py` already computes as its expected value, so
  the test becomes a tautology and should be replaced by a test of the `allowed_tools is None`
  case. `connector_specs` builds `ConnectorSpec` descriptions only — no client, no socket — so the
  original reason for not calling the builder ("constructing one opens an `httpx.AsyncClient`")
  died with `connector_tools`; the test at `:154` already says so in its own comment.
  Behaviour-preserving for every shipped manifest (measured: identical for all six profiles);
  it *changes* behaviour for the `tools: []` manifest, which is the point.

---

## `_narrow` is a one-caller helper with three dead parameters and a dead branch

- **Severity**: medium
- **Location**: `src/chemclaw/agent/chemclaw_agent.py:505-526` (`_narrow`); sole caller `:464`
- **Trigger**: reading it. `_narrow(tools, keep, profile_name, kind, also_known=None)` is documented
  as a generic in-process-tool narrower; after the MAF removal it narrows connector specs and
  nothing else.
- **Consequence**: four of the function's five parameters and one of its two lookup branches are
  unreachable, so a reader has to hold a generic contract in their head to understand a
  connector-name filter. The repo's own rule (`CLAUDE.md`, *No boilerplate*) is "delete dead params
  … on sight".
  - `also_known` — **never passed**, anywhere: `grep -rn "also_known" src tests` returns nothing.
  - `kind` — always the literal `"connector"`.
  - `t.__name__` in `getattr(t, "name", None) or t.__name__` (`:519`) — the only caller passes
    `ConnectorSpec`, a frozen dataclass with fields `name`, `connection`, `allowed_tools`
    (`connectors/transport.py:84-99`) and no `__name__`. The fallback cannot fire.
  - The docstring (`:512-517`) describes only the branch that cannot fire.

  Separately, `available = {…: t for t in tools}` silently deduplicates two connectors sharing a
  name rather than raising, which is the opposite of the fail-fast the rest of the function is for.
- **Evidence**:
  ```
  $ grep -rn "_narrow(" src tests --include=*.py
  src/chemclaw/agent/chemclaw_agent.py:464:  specs = _narrow(specs, prof.mcp_server_names, prof.name, "connector")
  src/chemclaw/agent/chemclaw_agent.py:505: def _narrow(
  $ grep -rn "also_known" src tests --include=*.py
  (no output)
  ```
- **Fix**: inline it into `connector_specs` as a connector-specific filter:

  ```python
  def _narrow_connectors(specs, keep, profile_name):
      available = {spec.name: spec for spec in specs}
      unknown = keep - available.keys()
      if unknown:
          raise ValueError(_unknown_names_message(profile_name, "connector", unknown, available))
      return [spec for name, spec in available.items() if name in keep]
  ```

  While there: `_reject_unknown_tool_names:434-437` and `_narrow:522-525` raise the same
  `f"agent profile {name!r} lists unknown {kind}(s) {sorted(unknown)}; known: {sorted(available)}"`
  message from two places — extract `_unknown_names_message`. Behaviour-preserving: yes (the removed
  branches are unreachable).

---

## `disabled_summarizer` re-solves a problem this repo already solved one module over — and does not disable the node

- **Severity**: medium
- **Location**: `src/chemclaw/agent/compaction.py:205-242` (`disabled_summarizer`), compare
  `src/chemclaw/agent/llm_provider.py:105-123` (`_CachingDisabled`)
- **Trigger**: build any agent (`build_langgraph_agent(model=…)`).
- **Consequence**: both functions exist to occupy an upstream middleware slot by `.name` so an
  upstream default does not apply. `_CachingDisabled` does it with a 4-line inert `AgentMiddleware`
  subclass. `disabled_summarizer` instead constructs the *real* `SummarizationMiddleware` with
  `trigger=None`, which costs three things the placeholder does not:

  1. **A live graph node.** The compiled graph carries `SummarizationMiddleware.before_model`, which
     runs on every model call, calls `_ensure_message_ids` (mutating the message list, assigning
     UUIDs) and a full `token_counter(messages)` pass, before `_should_summarize` returns `False`.
     Measured at the shipped 100k-token budget: **0.91 ms per model call** for a guaranteed `False`.
  2. **Two parameters threaded for nothing.** `_middleware` (`langgraph_agent.py:261-267`) takes a
     `model` argument used at exactly one call site — `disabled_summarizer(model, backend)`
     (`:313`) — and its docstring has a paragraph explaining that it is "needed only to construct
     the summarizer this list switches off". `build_langgraph_agent` threads `chat_model` down for
     it.
  3. **A lazy `langchain.agents.middleware.summarization` import** in the disabled path.
- **Evidence**: `/tmp/swap2.py` compiles the graph both ways and diffs the node set:

  ```
  with real SummarizationMiddleware(trigger=None): ['PatchToolCallsMiddleware.before_agent',
    'ReloadingSkillsMiddleware.before_agent', 'SummarizationMiddleware.before_model',
    '__end__', '__start__', 'model', 'tools']
  with inert placeholder                        : ['PatchToolCallsMiddleware.before_agent',
    'ReloadingSkillsMiddleware.before_agent', '__end__', '__start__', 'model', 'tools']
  difference: {'SummarizationMiddleware.before_model'}
  ```

  Upstream's splice is purely by name and replaces in place
  (`deepagents/graph.py::_apply_custom_middleware`), and `SummarizationMiddleware` contributes no
  tools and no extra state channels:

  ```
  name: SummarizationMiddleware
  state_schema S: _DefaultAgentState | base: _DefaultAgentState
  tools S: - | base: -
  hooks overridden: ['before_model', 'abefore_model']
  real._trigger_clauses  : []
  real.before_model([])  : None
  ```
- **Fix**: replace `disabled_summarizer(model, backend)` with the pattern already in the tree:

  ```python
  class _SummarizationDisabled(AgentMiddleware):
      @property
      def name(self) -> str:
          return "SummarizationMiddleware"
  ```

  Better still, since there are now two of these, make it one helper —
  `def inert_middleware(name: str) -> AgentMiddleware` — and have both `compaction` and
  `llm_provider` call it. Then drop `_middleware`'s `model` parameter and the paragraph explaining
  it, and stop passing `chat_model` down. Behaviour-preserving: yes for the model-facing contract
  (the hook is a proven no-op); it removes the `_ensure_message_ids` id-assignment side effect,
  which `add_messages` performs anyway.

---

## `response_format` is a parameter whose own docstring says nothing calls it

- **Severity**: low
- **Location**: `src/chemclaw/agent/langgraph_agent.py:127`, `:160-168` (docstring), `:230`
- **Trigger**: none — that is the finding.
- **Consequence**: a nine-line docstring paragraph and a parameter in the signature of the most-read
  function in the slice, for a passthrough with no caller. The docstring states it outright: *"It
  has no caller today, and is kept because it is a passthrough to `create_deep_agent`"*. The repo's
  own rule is "Delete dead params, empty interfaces, and 'for later' stubs on sight" and "No
  abstraction without a second real caller".
- **Evidence**:
  ```
  $ grep -rn "response_format" src --include=*.py
  src/chemclaw/agent/langgraph_agent.py:127:    response_format: Any | None = None,
  src/chemclaw/agent/langgraph_agent.py:160:        response_format: A pydantic model ...
  src/chemclaw/agent/langgraph_agent.py:230:        "response_format": response_format,
  ```
  The only other occurrences in the repo are `tests/test_prompt_caching.py:184` passing `None`
  explicitly, and `agent/verifier.py`'s unrelated use of the OpenAI request field.
- **Fix**: delete the parameter, the docstring paragraph and the `shared` dict entry. Re-add it with
  the caller that needs it — it is one line either way. Behaviour-preserving: yes;
  `create_deep_agent`/`create_agent` default it to `None`.

---

## The default system prompt is a 13 KB Python string literal while every other profile's is a data file

- **Severity**: low
- **Location**: `src/chemclaw/agent/chemclaw_agent.py:59-216` (`_INSTRUCTIONS`)
- **Trigger**: editing the prompt every unprofiled turn runs on.
- **Consequence**: the same content — an agent's instructions — is stored two ways. The six shipped
  profiles carry theirs in `data/profiles/*.yaml` under an `instructions:` key; the seventh, the
  default, is 13,187 characters of implicitly-concatenated string literals occupying 158 of
  `chemclaw_agent.py`'s 264 code lines (60% of the module). Editing it is a Python diff with
  hand-maintained trailing spaces and `\n`s; editing any other profile's is a YAML diff. The repo's
  own architecture rule is "`data/` holds every corpus the code reads at runtime".
- **Evidence**:
  ```
  $ ls data/profiles/
  computation.yaml design.yaml evidence.yaml property-lookup.yaml reporting.yaml safety.yaml
  $ grep -rn "^instructions" data/profiles/*.yaml
  (all six)
  $ uv run python -c "from chemclaw.agent.chemclaw_agent import _INSTRUCTIONS as I; print(len(I))"
  13187
  ```
  `/tmp/prose_ratio.py` counts `chemclaw_agent.py` at 264 code lines; `_INSTRUCTIONS` is 158 of them.
- **Fix**: add `data/profiles/default.yaml` with the same `instructions:` block and have
  `instructions_for` fall back to it. `profiles.py` deliberately imports neither `settings` nor
  `chemclaw_agent`, so the read belongs in `instructions_for` (cached with `@cache`), not in
  `DEFAULT_PROFILE`. Behaviour-preserving if the YAML round-trips byte-identically — assert that in
  a test during the move (`assert instructions_for(DEFAULT_PROFILE) == _INSTRUCTIONS` in the same
  commit, then drop the constant).

---

## The "note matches every query term" predicate is cloned

- **Severity**: low
- **Location**: `src/chemclaw/agent/graph_tools.py:113` and `src/chemclaw/durable/digest.py:96`
- **Trigger**: changing the match rule (e.g. to make it prefix-matching, or to skip a term class) —
  it must be changed in both places or `find_notes` and subscription digests silently disagree about
  what a query matches.
- **Consequence**: two byte-equivalent expressions of one rule, neither of which lives next to
  `term_coverage` in `kg/search.py` where the rule belongs.
  ```
  graph_tools.py:113   if terms and term_coverage(note, terms) == len(terms):
  digest.py:96         return bool(terms) and term_coverage(note, terms) == len(terms)
  ```
  `retrieval/retrievers.py:164` uses `term_coverage` for *partial* coverage scoring, which is a
  genuinely different question and correctly separate.
- **Evidence**: `grep -rn "term_coverage" src --include=*.py`, output above.
- **Fix**: add `def matches_all_terms(note: Note, terms: Sequence[str]) -> bool` to
  `chemclaw/kg/search.py` beside `term_coverage`, and call it from both sites.
  Behaviour-preserving: yes (`terms` truthiness and `bool(terms)` are the same test on a list).

---

## `history_provider` does not belong in `chemclaw_agent.py`

- **Severity**: low
- **Location**: `src/chemclaw/agent/chemclaw_agent.py:253-270`
- **Trigger**: looking for where session persistence is selected.
- **Consequence**: the module's own docstring scopes it — *"What one profile advertises: the
  instructions, the tools, and the connectors"* — and this function answers none of those three. It
  picks between `InMemoryHistoryProvider` and `PostgresHistoryProvider` from `settings.session_store`
  and is imported by `api/app.py:38`. It carries a lazy import comment ("so nothing pays for psycopg
  at import time") that exists only because it is in the wrong module: `session_store.py` already
  imports psycopg unconditionally.
- **Evidence**:
  ```
  $ grep -rn "history_provider" src --include=*.py
  src/chemclaw/api/app.py:38:  from chemclaw.agent.chemclaw_agent import connector_specs, history_provider
  src/chemclaw/api/app.py:251: app.state.history = history_provider()
  ```
  One production caller, in a module that imports `chemclaw_agent` for a different reason.
- **Fix**: move it to `chemclaw/agent/session_store.py` (which defines both branches) and drop the
  lazy import. Behaviour-preserving: yes, one import line changes in `api/app.py`.

---

## Not findings — checked and cleared

Recorded so a later round does not re-derive them:

- **Per-turn graph compile cost (measured 173 ms here, 113 ms parent + 60 ms helper).** Already
  measured, bounded and argued in `tests/test_langgraph_connectors.py:323`, including the exact
  remaining lever (`_subagents` not passing `labelled` down) and the reason it was declined.
- **`graph_tools` calling `build_graph(settings.knowledge_path)` at four sites.** `kg.graph.build_graph`
  is cached behind a stat fingerprint and returns a frozen graph; measured 0.0 ms warm over the
  38-note corpus. Not a duplication worth extracting.
- **`_register_generated_tools` mutating the process-global tool registry on every build.**
  `registered_tool_names()` grows 20 → 28 on the first build, but `available_tool_names()` is
  stable at 66 before and after, because the template and connector name spaces already cover the
  generated launchers. No order-dependent answer.
- **`skills_backend`'s optional `labelled=` and its public visibility exist only for tests.** True
  (no non-test `src/` caller), but the parameter genuinely removes a duplicated filesystem walk on
  the real path, so the shape is earned.
- **`tool_governance_middleware` / `tool_call_middleware` split.** Both have real, distinct callers
  (`agent/tool_invocation.py:160` and `langgraph_agent._middleware:319`) and the split is what keeps
  the model-facing converters off the model-less template path. Correctly factored.
