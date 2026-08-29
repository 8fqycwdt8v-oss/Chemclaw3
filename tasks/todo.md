# Subagents: a helper that is cheaper and narrower than its caller (A + B), and a look at C/D/E

Context: `docs/decisions/D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` deleted the
specialist team as unreachable code, and the delegation *question* was never settled — the two
measurements it rests on (2/15 and 14/15-vs-14/15) were taken on different harnesses, one sample per
probe, against the wrong dependent variable (delegation rate rather than cost/quality per task).
What ships today is one governed `general-purpose` helper built from its caller's *own* profile:
same model, same instructions, and — measured against the live registry — the same 54 in-process
tools, including nine durable job launchers, `propose_knowledge_note`, `start_optimization_campaign`
and `request_external_input`. The helper's own `task` description says it is for isolation and
parallel reading. Its surface says otherwise.

## A — a profile may name the model route it runs on

- [x] `AgentProfile.model_route: str | None` — a key into the existing `settings.model_routes`
      table, **not** a model id (a model id in a checked-in profile YAML would hardcode a site's
      model names into this repository; `model_routes` is the seam that exists to avoid that).
- [x] `build_langgraph_agent` resolves it through `build_chat_model(task=...)`, the one place a
      model is built. Unrouted key ⇒ today's behaviour byte-for-byte (no second client).
- [x] The helper's derived profile carries `model_route="helper"`, so
      `CHEMCLAW_MODEL_ROUTES='{"helper":"internal-small"}'` is the whole cost lever.

## B — a helper holds only what its story claims

- [x] `helper_profile(caller)` in `agent/subagents.py`: the caller's profile minus
      `authz.side_effecting_tools()` — derived from the existing maintained partition, so a bundle
      added next year is excluded on the day it is enabled, not the day someone remembers.
- [x] Minus `ask_clarifying_question` as well: it is classified read-only and it writes a turn
      signal, so a helper calling it puts a question on the *chemist's* stream from a context the
      chemist cannot see, and never sees the answer.
- [x] Applied inside `build_langgraph_agent(helper=True)` rather than in `_subagents`, so the
      narrowing cannot be forgotten by a future caller.
- [x] `HELPER_BRIEF` appended to the helper's system prompt, next to the `task` description in the
      same module — the two texts a helper is defined by, side by side, with a test that they agree.
- [x] Update the `task` description: "holds the same in-process tools you do" becomes false.

## Verification

- [x] `tests/test_subagents.py`: attenuation becomes a *strict* subset; new tests for the
      side-effecting exclusion, the chemist-facing exclusion, the routed/unrouted model, and the
      two texts agreeing.
- [x] A scan test deriving the chemist-facing set, so a second such tool fails the suite.
- [x] `make lint type test`, reporting what the run skipped.

## Then: investigate C, D, E and report whether to build

- [x] C — per-helper connector sessions (is the deadlock a *sharing* hazard or a concurrency one?)
- [x] D — an advisor (a consult that holds no tools; the invariant never contemplated it)
- [x] E — a second roster name

## Review

**A and B are implemented, measured and merged into one change.**

*A landed as `AgentProfile.model_route` rather than `AgentProfile.model`, and that is the one
deviation from the plan as asked.* A model *id* on a profile would put a site's model names into a
checked-in `data/profiles/*.yaml`, which is precisely what `settings.model_routes` — the per-task
routing table `build_chat_model(task)` already reads — exists so that nobody has to do. A route
*key* keeps the deployment's answer in the deployment. It also gave the field a second real caller
without inventing one: a session profile may name a route too.

*B landed as a subtraction of `authz.side_effecting_tools()` rather than a hand-written allow-list*,
because that partition already exists, is already assembled from three sources that own their own
knowledge, and is already held to the tool registry by `tests/test_authz.py`. A list in
`agent/subagents.py` would have been a fourth source, correct on the day it was written.

**Measured, on the default profile against the two compiled graphs:** the caller binds **61** tools,
the helper binds **24**. The difference is nine `run_*` durable job launchers, every knowledge-graph
and preference write, `request_external_input`, `ask_clarifying_question` and `task`. Nothing
widened. Before this, the two surfaces were identical, which is why the attenuation test could not
have failed — it is now a strict subset.

**One thing found while implementing, and it changed the design.** The first version read the tool
registry inside `helper_profile`. That is wrong ordering: the registry is complete only after
`_capability_tools` has run `_register_generated_tools()`, so a set read earlier is missing every
launcher a deployment generated. It gave the right answer today — those are all side-effecting and
subtracted anyway — for a reason that stops being true the first time a generated tool is a read.
The caller's resolved names are now passed in, and the call site says why.

**One test was rewritten rather than added.** The first version of the routed-model test read the
chat model back off the compiled graph; LangGraph's model node is a closure, and prising the client
out of it would have been a seventh reader of a shape upstream never promised — the thing
`tests/test_upstream_surface.py` exists to count. The tests now assert what
`_resolve_chat_model` actually claims, which is about construction: a routed profile builds from its
route, an unrouted one builds nothing because a usable client already exists.

**Gate:** `make lint` clean, `make type` clean (795 files), `make test` **6241 passed, 14 skipped**
— run with `dockerd` and `make up` first, so the Postgres-backed suite actually ran. The 14 skips
are `helm` not installed (7), a truncated git history the migration-additivity checks cannot use
(3), the two live prompt-caching probes whose Anthropic credential has no credit, and two others in
the same families — not the ~216 that a suite run without Postgres would have skipped silently.

**C, D and E were investigated and none is built.** Findings are `docs/planning/BACKLOG.md` rows in
§4, each with what would change the answer:

- **C (per-helper connectors): the stated reason is not the binding one.** The deadlock measurement
  is about *sharing one session object*, not concurrency — a helper with its own sessions shares
  nothing. What actually binds is the lifecycle: connectors are opened by the async caller into an
  exit stack *before* the synchronous `build_langgraph_agent` runs, and the roster is frozen per
  compiled graph, so a per-helper set means opening a second full set eagerly on every turn against
  an unmeasured spawn rate. Fix the prose now; let the measurement decide the behaviour.
- **D (an advisor): permitted by every merged decision, and the design is already determined.**
  `D-2026-08-25`'s thread-versus-tool table resolves all seven rows in an advisor-as-tool's favour,
  which is the injection objection that killed the summarizer three times; `agent/condense.py` is
  the precedent for the in-tool model call, including how its spend reaches the cap. It is blocked
  on a second model tier and on evidence, not on architecture.
- **E (a second roster name): mostly eaten by B.** The case for it was a read-only reader beside a
  full-capability helper; the only helper is now read-only. Leave closed until the measurement asks.

**And the delegation question is still open.** This change does not answer it and does not claim to.
The row that would is in §4, with the arms it needs — which exist as of this change.
