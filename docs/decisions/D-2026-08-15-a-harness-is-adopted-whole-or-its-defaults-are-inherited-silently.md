# D-2026-08-15-a-harness-is-adopted-whole-or-its-defaults-are-inherited-silently — layer 1 compiles on `create_deep_agent`, and every default it brings is answered for

**Status:** accepted · **Date:** 2026-08-15 · Supersedes the `create_deep_agent` declination in `D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has` and the "not yet" in `D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work`. Keeps both of their objections and pays them.

## Context

`D-2026-08-11` declined the bundled harness on one ground, and it was the right ground: its default
stack "always registers `FilesystemMiddleware` — a write/edit/glob/grep surface, plus shell", every
name of which "would then have to be answered for by `available_tool_names`, gated by
`tool_role_gates` and justified in the safety rubric — a general filesystem acquired as a side
effect of wanting to read a `SKILL.md`."

The scratchpad ADR paid exactly that cost the same day, deliberately and by hand: six filesystem
verbs entered `skill_tool_names`, `execute` and `delete` were withheld, and the middleware was
composed onto `create_agent` rather than obtained from `create_deep_agent`. It listed four reasons
to stay on the hand-assembled list. Three of them dissolved on measurement and the fourth was
false — the correction blockquote in that ADR records it, and this one records what was found in its
place.

What remained unreachable from the hand-assembled position was not cosmetic. **`permissions=` has no
public seam on `FilesystemMiddleware`** — `_permissions` is private — so `filesystem_permissions()`
was written and unenforced. And `subagents=` is the only way the `task` tool's roster is decidable
at all.

## Decision

Compile on `create_deep_agent`, and treat every default it brings as a decision this repository
makes rather than one it receives.

**The splice rule is the mechanism, and both halves of it are used.**
`_apply_custom_middleware` replaces an entry whose `.name` matches one upstream already composed,
and lands a new name immediately after the last core member. So the list `_middleware` returns is
not a stack — it is two instructions — and reading it as a sequence is the mistake
`tests/test_middleware_order.py` exists to catch. Exactly two entries are replacements:

- **`FilesystemMiddleware`**, because upstream composes one unconditionally with all eight verbs.
  Ours carries `tools=scratchpad_tools()`, which is where `execute` and `delete` are withheld.
- **`SummarizationMiddleware`**, because upstream composes one of those unconditionally too — see
  the separate decision below.

Everything else lands as a block after the last tool-registering middleware, which is what keeps the
seven governance wrappers *inside* `FilesystemMiddleware` and `SubAgentMiddleware`. That position
used to be arranged by putting them first in a hand-built list; it is now arranged by somebody
else's splice rule, which is precisely why it is asserted rather than assumed.

**`HarnessProfile` is not used for any of it, and the reason is a measurement.** Both narrowings
above could be expressed as `excluded_tools` / `excluded_middleware` on a registered profile. A
profile is resolved by the model's self-reported `provider:identifier`, and on a key miss it is
*silently* not applied — reproduced during this work, with one warning logged and upstream's
defaults left in place. A security narrowing that fails open on a model swap is not a narrowing.
Sharing a `.name` is a plain string comparison with nothing to mismatch.

**`skills=` is deliberately not passed.** Upstream composes a `SkillsMiddleware` only when it is,
so withholding it leaves exactly one — `ReloadingSkillsMiddleware`, which re-narrows per caller —
with no splice to arrange and no second source list to keep in step.

## The `task` tool was never optional

`SubAgentMiddleware` is in upstream's `_REQUIRED_MIDDLEWARE`, and `_apply_excluded_middleware`
**raises** rather than let a profile strip it. So adopting the harness registers `task` whether or
not this deployment wants helpers, and the only decision available is what it reaches.

Left alone, `create_deep_agent` inserts its own `general-purpose` subagent holding every tool the
parent holds, assembled from upstream's middleware — no audit row, no authorization gate, no dry-run
refusal, no plan gate. `D-2026-08-13` recorded the consequence: "nothing would fail while it did".

Two suppressions exist and **only one is reliable**, measured across three arms:

| arm | result |
|---|---|
| defaults | the `task` roster lists upstream's ungoverned `general-purpose` |
| a supplied spec claiming that **name** | ours replaces it; upstream's is gone |
| a supplied spec under a different name | upstream's stays, **beside** ours |

`GeneralPurposeSubagentProfile(enabled=False)` is the other route and travels through the same
fragile profile lookup. So `agent/subagents.py` claims the name, and the roster is one entry:
a `CompiledSubAgent` whose runnable is a graph from `build_langgraph_agent`.

**One helper rather than five.** `D-2026-08-13` framed the roster as five tool surfaces; M12
measured 2/15 delegation on the framing before it and a later run put both framings at 14/15 on the
same corpus, concluding the reframing bought nothing detectable and that neither number is the
deployment's rate. A named partition is a routing hypothesis nobody here has measured on real work.
Fan-out needs none: `task` already tells the model to launch several agents concurrently when their
tasks are independent.

**A helper holds no connector tools**, which is a concurrency bound rather than an attenuation: a
helper is concurrent with its caller by construction, and two concurrent turns over one MCP tool
object deadlock — the measurement that already explains why a graph is compiled per turn.

**A helper compiles on `create_agent`, and that is a correction the measurement forced.** The first
version routed both through `create_deep_agent` and returned an empty roster for a helper. That is
not what "no helpers" means to upstream: with no spec claiming the name it inserts its own, so the
recursion guard grew the ungoverned `task` surface it exists to prevent, one level down. Reading the
compiled middleware list is what found it — no test would have.

**`reject_widening` did not come back as a function**, and the ADR that promised it should be read
as promising the invariant rather than the symbol. Under a one-name roster the helper is built from
its caller's own profile, so comparing the two *declarations* compares a value with itself and could
never turn red. `tests/test_subagents.py` compares the two *compiled* tool surfaces instead, which
is the difference between enforcing an attenuation and restating one.

## The summarizer is declined again, this time explicitly

`create_deep_agent` composes a `SummarizationMiddleware` unconditionally. D-025 declined a summarizer
and `agent/compaction.py` has carried the argument since; while the list was hand-assembled, "no
summarizer" was expressed by not importing one. It now has to be *made* rather than merely not
unmade.

The deepagents variant genuinely answers half the objection: evicted messages are written to a path
the summary embeds, so evidence stays readable instead of being dropped, and `state["messages"]` is
left intact. It cannot answer the other half. `agent/framing.py` wraps untrusted tool output in an
envelope that marks it untrusted; a summary is new model prose written *over* that content, and the
envelope does not survive it. Retrieved text that arrived flagged as external returns as unflagged
narration and is re-read every subsequent turn.

`disabled_summarizer` constructs upstream's own class with `trigger=None` — its documented off state
(`_should_summarize` opens `if not self._trigger_clauses: return False`) — and splices it in by name.
`tests/test_compaction.py` asserts the *behaviour* rather than the constructor argument, because both
instances report the same `.name` and the order list cannot tell them apart; the assertion was
verified to fail with the replacement removed rather than assumed to.

## Two claims checked because the prose asserted them

- **The return is a `CompiledStateGraph`, not a `RunnableBinding`.** It ends `.with_config(...)`, but
  `Pregel.with_config` returns a *copy of the graph*, so `aget_state` is present. The earlier claim
  was reached by checking a proxy (does the source call `.with_config`?) instead of the claim.
- **`turn_config`'s ceiling still binds** over the second 9999 `create_deep_agent` bakes onto the
  graph. A model scripted to call one tool forever raised `GraphRecursionError` reporting "Recursion
  limit of 153". Config passed at `ainvoke` wins over the graph's own.

## Consequences

- `task` becomes a sixth tool name space (`subagent_tool_names`), derived from the middleware rather
  than spelled, so the four validators answer for it. It survives every profile narrowing, and that
  is correct rather than an oversight: the helper is built from the same profile, so `task` confers
  no authority its caller does not already hold — the same argument `read_file` stands on.
- `SummarizationMiddleware` and `PatchToolCallsMiddleware` sit *outside* the governance wrappers.
  Neither executes a tool: the first clips and offloads a returned result, the second repairs a
  malformed call. The audit row is written on the call itself either way.
- **Honest limit: `permissions=` is now passed and not proven.** The rules reach upstream, and no
  test yet drives a write to a denied path through the compiled graph. The refusal that is proven is
  `NarrowedSkillsBackend`'s, which is where D-2026-08-10 put it and where it is load-bearing anyway.
- The unpromised-upstream-shape count grew by three, all in `tests/test_upstream_surface.py`:
  `_apply_custom_middleware`'s splice rule, `_REQUIRED_MIDDLEWARE_NAMES`, and
  `GENERAL_PURPOSE_SUBAGENT["name"]`. Each names the first-party module that breaks. The
  `HarnessProfile` assertion they replaced described a mechanism this repository decided not to use.
