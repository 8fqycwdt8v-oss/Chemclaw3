# Upstream gaps

Every place this repository works around, reaches into or pins a shape of `deepagents`, `langchain`
and `langgraph`, classified against the release installed here and the latest release on PyPI. The
decision behind it is to stay on off-the-shelf `deepagents` (`D-2026-10-07-the-architecture-programme`):
for each workaround the question is which public seam does the job, and if there is none, whether
upstream can take it.

| Class | Meaning | What happens |
| --- | --- | --- |
| **(a)** | A public API now covers it | Migrate, in the same change as the dependency bump |
| **(b)** | A documented extension point covers it (`AgentMiddleware`, `BackendProtocol`, `CompiledSubAgent`, `context_schema`, `state_schema`) | Migrate, or record why not |
| **(c)** | A real upstream gap | Draft an upstream issue (`upstream-issues/`), keep a minimal shim that links it, and give the shim a test that turns red when upstream fixes it |
| **keep** | Not a workaround: a contract the code reads, a restart condition, or a third-party shape | The pin stays |

Versions compared (checked against PyPI on 2026-10-14):

| Package | Installed | Latest | Package | Installed | Latest |
| --- | --- | --- | --- | --- | --- |
| `deepagents` | 0.7.8 | 0.7.25 | `langchain-openai` | 1.6.0 | 1.7.2 |
| `langchain` | 1.3.16 | 1.4.6 | `langchain-mcp-adapters` | 0.3.2 | 0.3.2 |
| `langchain-core` | 1.6.0 | 1.6.10 | `langgraph-checkpoint-postgres` | 3.1.2 | 3.2.0 |
| `langgraph` | 1.2.11 | 1.2.15 | `langgraph-checkpoint` | 4.2.0 | 4.3.0 |
| `langgraph-prebuilt` | 1.1.0 | 1.1.0 | `mcp` (capped `<2`) | 1.30.0 | 1.30.0 (2.3.0 is past the cap) |

`uv lock --upgrade-package …` resolves all of them together without touching a floor in
`pyproject.toml`. Upstream's release notes were read from the repository's changelog
(`libs/deepagents/CHANGELOG.md`); the sources of both releases were diffed.

## Where the "latest" column comes from

Each pin in `tests/test_upstream_surface.py`, and the tests that exercise the agent builder, the
subagents, skills, the plan gate, authorization, audit, streaming, context budgeting and turn
durability, were run serially against the latest release with no source change. Seven tests turned
red; two are pins (named in the tables) and five are first-party tests that measured upstream's own
behaviour:

- `test_middleware_order`: `create_deep_agent` appends a new `UnsupportedContentMiddleware`
  (it replaces a content block the model's profile explicitly rejects with a text notice).
- `test_skill_backend`: `read_file` reports truncation as a header (`@@ lines 1-100 of 212 | next
  offset 100 @@`), not the sentence the test matched.
- `test_checkpointer_prune`: a turn writes more checkpoints, because `SkillsMiddleware` now has a
  `before_model` hook and every hook is a node (draft 04). The test's bound of 20 was a measured
  constant; it should be derived from one turn's writes.
- `test_context_floor`: the `task` schema is 929 tokens against a bound of 900, from upstream's
  longer usage notes. This is the case the `write_todos` entry in `KNOWN_OVERSIZED` already records.
- `test_model_text_inventory`: the committed inventory of everything a model reads lists
  `edit_file`, `read_file` and `task`, which changed.

Separately, the model-facing text upstream owns was diffed (see
[Upstream text the model reads](#upstream-text-the-model-reads)). Everything else in the gate list,
including the multi-process `tests/test_turn_survives_pod.py`, passed.

## Workarounds in `src/chemclaw/agent/`

| Workaround | Where | Class | Installed | Latest | Disposition |
| --- | --- | --- | --- | --- | --- |
| Replace `FilesystemMiddleware` by `.name` to withhold `execute` and `delete` with `tools=[…]` | `langgraph_agent._middleware`, `scratchpad.scratchpad_tools` | **(a)** | The splice is documented (`create_deep_agent` docstring); `tools=` is public | Unchanged | Keep. Fully public. Becomes unnecessary only with draft 03 |
| `permissions=` on that replacement | `langgraph_agent._middleware`, `scratchpad.filesystem_permissions` | **(a)** | Public keyword since `deepagents` 0.7.31 (draft [02](upstream-issues/02-deepagents-permissions-do-not-reach-a-replaced-filesystem-middleware.md) is fixed upstream) | Public | Migrated with the bump to 0.7.31 |
| `permissions=filesystem_permissions()` on the outer `create_deep_agent` call | `langgraph_agent.build_langgraph_agent` | **(a)** | Dead: it reaches only the instance the replacement displaces, no declarative subagent and no `interrupt` rule | Dead | Remove with the bump (W5.2); `tests/test_scratchpad.py` still drives an out-of-bounds write through the compiled graph |
| `disabled_summarizer`: replace the summarizer by name with `SummarizationMiddleware(trigger=None)` | `compaction.disabled_summarizer` | **(a)** for the splice, **(c)** for the lever | Documented splice. `backend=` is swallowed by `**deprecated_kwargs`: a dead parameter. `HarnessProfile.excluded_middleware` exists but is resolved by the model's provider key and misses silently ([03](upstream-issues/03-deepagents-a-harness-profile-per-agent.md)) | Same | Remove the dead `backend` parameter with the bump (W5.2). The splice stays until draft 03 lands. The replaced class is deepagents' own, so its new overflow recovery is not in play; compaction is first-party |
| `ReloadingSkillsState`: redeclare `skills_metadata` as `UntrackedValue` so the listing reloads each run | `langgraph_agent.ReloadingSkillsState` | **(c)** | Needs `PrivateStateAttr`, which is not in `types.__all__`. Its public equal `OmitFromSchema(input=True, output=True)` is **not** a substitute: `deepagents` finds the keys it keeps out of a subagent by identity (`meta is marker`), so the equal object would let the key cross the `task` boundary | Since 0.7.16 `skills_metadata = None` requests a reload and `before_agent` honours it. A reload on every run then rests on every entry point (`api/graph_stream.py`, `durable/template_activities.py`, `cli/chat.py`, resumes) passing `None`, and the listing is checkpointed again | Keep the channel: "per run" is a property of the channel, not of callers who remember. Draft [04](upstream-issues/04-deepagents-skills-middleware-run-scoped-listing-and-hooks.md) asks for it on the middleware, and for `PrivateStateAttr` to be exported |
| `_format_skills_list` override for an empty listing | `langgraph_agent.ReloadingSkillsMiddleware` | **(c)** | Private method | Private method | Draft 04. The pin turns red when the method is renamed |
| `_SKILLS_PROMPT_REMOVALS`: cut passages from `SKILLS_SYSTEM_PROMPT` by substring | `langgraph_agent._skills_prompt` | **(b)** | Passed through the public `system_prompt=`; the constant is not in `__all__` | Template unchanged; all four passages found | Keep. The alternative, a first-party template, forgoes upstream's wording on every bump; revisit if upstream parameterises it (draft 04) |
| `.name` splice of first-party middleware into upstream's stack | `langgraph_agent._middleware` | **(a)** | Documented behaviour (changelog 0.7.0, `create_deep_agent` docstring); the pin drives a private function | Documented, same rule | Re-point the pin at the public path with the bump (W5.2) |
| Claim `general-purpose` so upstream inserts no ungoverned helper | `subagents.general_purpose_helper` | **(a)** | Documented override ("if no subagent named `general-purpose` is provided …") | Same. `GeneralPurposeSubagentProfile(enabled=False)` removes it without a claim, again through a provider-keyed profile | Keep |
| Helpers as bare `SubAgent` dicts with only `spec["middleware"]` | `langgraph_agent._subagents` | n/a | **Not true at HEAD.** Every helper is a `CompiledSubAgent` whose `runnable` is `build_langgraph_agent(helper=True)`, compiled with `checkpointer=False` through `create_agent` and carrying the whole chain; `subagents.governed_roster` refuses any spec without a runnable | Same. The only difference from the main agent is that a helper is not built by `create_deep_agent`, so it lacks upstream's tail middleware (`PatchToolCallsMiddleware`, prompt caching), which a one-prompt, one-report graph does not need | Closed; nothing to migrate |
| `ignoring_inherited_durability` | `core/graph_durability.py` | **(c)** | `durability="sync"` is inherited by a graph with no saver and fails with `AttributeError` | Reproduced unchanged on langgraph 1.2.15: [01](upstream-issues/01-langgraph-sync-durability-without-a-checkpointer.md). `Pregel._defaults` was refactored (the pin that grepped its source turned red) but still returns `"sync"` for a saver-less graph | Shim keeps working on 1.2.15. Rewrite the pin to the behaviour with the bump (W5.2) |
| `task` returns a `Command` carrying the helper's whole state | `tool_result_shape` | **(b)** | Rewritten in a `wrap_tool_call` middleware (a documented seam); the pin reads the source of a private function | Same shape (`Command(update={**state_update, "messages": […]})`) | Keep the middleware. Pin: drive a real `task` call instead of grepping `_build_task_tool` |
| Runaway cap in `before_model` instead of `ModelCallLimitMiddleware` | `guard.enforce_loop_cap` (`spend/guard.py`) | **(c)** | Upstream counts in `after_model`, which a jump from a later hook skips | Unchanged. Repro: [06](upstream-issues/06-langchain-model-call-limit-is-skipped-by-an-after-model-jump.md) | Keep first-party. Not a shim on upstream's behaviour: upstream's limiter is unsafe here |
| `OffLoopContextEditing` | `compaction.OffLoopContextEditing` | **(c)** | `awrap_model_call` deep-copies and edits on the event loop | Unchanged. Repro: [05](upstream-issues/05-langchain-context-editing-runs-on-the-event-loop.md) | Shim stays |
| `ScopedTodoListMiddleware` (each step declares its tools) | `plan_scope` | **(b)** | A subclass of the public `TodoListMiddleware`; reads its `system_prompt`, `tool_description` and `tools` attributes and re-implements the private `_write_todos` | Same | Keep; pins hold |
| `ChemclawState` extends `PlanningState`, not `DeepAgentState` | `state` | **(b)** | `state_schema=` is public and the docstring says to extend `DeepAgentState`, whose `messages` channel is a `DeltaChannel`. Without it every superstep rewrites the whole thread (`checkpointer._prune_superseded` notes the quadratic volume) | Same. A synthetic 12-tool-call turn writes 56 kB with `DeepAgentState` against 764 kB with a plain `AgentState` (13.6×, in-memory saver; `AsyncPostgresSaver` ships the delta read path) | `DeltaChannel` is still marked beta in langgraph 1.2.15 (`BACKLOG.md`, "Gated on an upstream fix"). **Not migrated**: it changes what a checkpoint holds, which `session_fork`, `message_migration`, the prune statement and the planned transcript projection all read, and upstream's own `aprune(keep_latest)` refuses such a thread. Decision for W5.8 |
| Checkpoint retention: delete superseded checkpoints inside the turn's own write | `checkpointer._PRUNE_SUPERSEDED`, `durable/retention.py` | **(b)** | Not available | `langgraph-checkpoint-postgres` 3.2.0 adds `AsyncPostgresSaver.aprune(thread_ids, strategy="keep_latest" \| "delete")`. `keep_latest` keeps exactly one checkpoint per namespace (this repository keeps `checkpoint_retain_per_thread`), runs as its own `REPEATABLE READ` transaction that can fail with `SerializationFailure`, and refuses a thread using a `DeltaChannel`. `delete` is `adelete_thread`, already used | Keep first-party. Revisit with the `DeltaChannel` decision: adopting one makes upstream's prune unusable and this repository's statement delta-aware, or the reverse |
| `UnsupportedContentMiddleware` appended to the stack | `create_deep_agent` | keep | Not present | New upstream tail entry. It replaces a content block the model's profile *explicitly* rejects with a text notice and passes everything else, so it changes nothing for a text-only thread | Add to the recorded order in `tests/test_middleware_order.py`; one more root middleware for the W5.3 count |
| Helper `files` cross back into the caller's state | `tool_result_shape.rewritten_command_files`, `tool_result_size` | **(b)** | `_EXCLUDED_STATE_KEYS` is a private constant; `files` is absent from it by design (`D-2026-09-04-a-helpers-file-crosses-back-and-stays`) | Same; `SubAgentMiddleware` exposes `subagent_names` and takes `private_state_keys`, not an excluded-keys parameter | Keep. Two pins turn red if `files` is excluded or a second producer appears |
| Scratch and memory backends | `scratchpad.BoundedStateBackend`, `BoundedStoreBackend`, `skills/backends.py` | **(b)** | Subclasses of the public backends; each forwards keyword-only arguments so no copy of upstream's signature goes stale | Same. Backends write an `encoding` key on file data now | Keep |
| First-party invalid-tool-call handling | `model_calls.PromoteInvalidToolCalls`, `refuse_unparsed_arguments` | **(a)** to check | `create_agent` does not repair invalid tool calls | `create_agent` now answers an unanswered `invalid_tool_calls` entry with its own `ToolMessage` ("… arguments were malformed or truncated") before the model call. The first-party promotion runs on the response and turns the call into a real one, so a call it promoted is not left for upstream to repair; `tests/test_invalid_tool_calls.py` passes unchanged | Keep; revisit if both ever see the same call |

## Pins in `tests/test_upstream_surface.py`

Grouped by what they read. "Latest" is the result of running the pin on the latest release.

### langchain: the plan (todo) middleware

| Pin | Kind | Latest |
| --- | --- | --- |
| `todos` is the plan channel; `Todo.status` has `in_progress`; `write_todos` is the tool name; `Todo` carries only `content` and `status` | keep: public types and a documented tool name | Holds |
| `TodoListMiddleware` lets a subclass read `system_prompt` and `tool_description` and replace `tools` | keep: attributes on a public class | Holds |
| A subagent cannot see the parent's `todos`; a subagent's `files` still cross into its caller's state | keep: a private constant read as a restart condition | Holds |

### deepagents: construction

| Pin | Kind | Latest |
| --- | --- | --- |
| `create_deep_agent` still takes the parameters the harness is built from; the seven names `src/` imports are importable | keep: public API | Holds. New parameters (`context_schema`, `memory`, `cache`) |
| Custom middleware replaces an upstream entry by name | (a): pinned through the private `_apply_custom_middleware` | Holds. Re-point it at the public behaviour (W5.2) |
| `SubAgentMiddleware` still cannot be excluded; claiming `general-purpose` still displaces upstream's | keep. With a `HarnessProfile` the `task` tool can now be dropped, which is the (c) lever of draft 03 | Holds |
| `FilesystemMiddleware` takes `permissions` | keep: public API (draft 02, fixed upstream) | Holds |
| The filesystem tool surface is the eight names; both write verbs take `file_path`; permissions have the modes `allow` and `deny` | keep: the gate answers for every verb | Holds |
| Oversized results are offloaded (`tool_token_limit_before_evict`); the store backend takes a namespace factory | keep: public constructor parameters | Holds |
| `read_file` has no video route | keep: a restart condition, read through the private `_video` module | Holds |
| The interrupt predicate is synchronous | keep: restart condition for `HumanInTheLoopMiddleware` | Holds |
| `SubAgentMiddleware`'s `task` closes over `subagent_graphs` | (a): test-only walk of a closure; `SubAgentMiddleware.subagent_names` is public for the names | Holds. Derive the helper from the spec first-party code built (W5.2) |
| `task` returns a dict-shaped `Command.update` | (b) | Holds; rewrite the pin to drive the tool (W5.2) |
| A helper's file is a mapping carrying its text under `content` | keep: `FileData` | Holds |
| Only the subagent middleware originates a `Command` carrying `files` | keep: scan of upstream source | **Red on latest**: it listed sites by line number, and the filesystem module grew. Name the enclosing function instead (W5.2) |

### deepagents: skills

| Pin | Kind | Latest |
| --- | --- | --- |
| The cache channel is `skills_metadata` and is omitted from the schema | **(c)** draft 04 | Holds. The marker is now `OmitFromOutput` upstream; the redeclaration keeps the stricter `PrivateStateAttr` |
| `PrivateStateAttr` is importable from `langchain.agents.middleware.types` | **(c)** draft 04: not in `__all__`, and matched by identity | Holds |
| `_format_skills_list` exists; `before_agent` takes the three-argument call the floor makes | **(c)** draft 04 / test-only | Holds |
| `SKILLS_SYSTEM_PROMPT` still contains each passage this deployment removes | **(b)** | Holds |
| An oversized skill description is truncated, not refused | keep: a private parser read as a restart condition | Holds |

### langchain and langgraph: the graph

| Pin | Kind | Latest |
| --- | --- | --- |
| `create_agent` bakes a recursion limit of 9999; the compiled agent names its tool node `tools` and reaches it at `nodes["tools"].bound`; a streamed message names its node | keep: the shapes `api/graph_stream.py` and the context floor read | Holds |
| `ToolNode` holds its tools under `_tools_by_name` | (a): `ToolNode.tools_by_name` is public | Holds. Migrate to the public property (W5.2) |
| `ToolNode` stores a prebuilt `BaseTool` as the same object | keep: behaviour the per-process schema cache relies on | Holds |
| `astream` with a mode list and `subgraphs=True` yields three-tuples | keep: documented stream format. A newer `version=` stream exists; the v3 transformer pin is the restart condition for moving | Holds |
| `ToolMessageChunk` is a `ToolMessage`; `ToolMessage.content` admits a list and a string; `BaseMessage.text` is a property | keep: public types | Holds |
| A dangling tool call is healed before the model reads it | keep: behaviour of `PatchToolCallsMiddleware`, driven through the production builder | Holds. `create_agent` also repairs `invalid_tool_calls` now |
| `ModelCallLimitMiddleware` counts in `after_model` | **(c)** draft 06 | Holds; turns red when moved |
| `ContextEditingMiddleware.wrap_model_call` hands its edited request to the handler once; a cleared result is marked in `response_metadata` | **(c)** draft 05 for the first; the second is a semi-public stamp | Holds |
| A subgraph compiled without a checkpointer inherits its parent's; `False` opts out | **(c)** draft 01 | **Red on latest**: it matched the literal `if self.checkpointer is False` in `Pregel._defaults`, which now calls `_resolve_checkpointer`. Rewrite it to the behaviour (W5.2) |
| `durability` is a parameter and `_defaults` resolves `"sync"` for a graph with no saver | **(c)** draft 01 | Holds |
| Every compiled graph in `src/` names its checkpointer | keep: a first-party scan | Holds |
| A checkpointer can `adelete_thread`; `aput` writes blobs and its row in one transaction; the store names its two version ledgers | keep: persistence contracts | Holds on `langgraph-checkpoint-postgres` 3.2.0 |
| A store search pages by offset and dates every item | keep: public store API | Holds |

### Gateway, MCP and the rest

| Pin | Kind | Latest |
| --- | --- | --- |
| The gateway client publishes cache tokens under two flat keys; a failed call carries the gateway's usage block | keep: `usage_metadata` is public; the pins fabricate input through two private helpers (`_create_usage_metadata`, `_generate_response_from_error`) | Holds |
| A pydantic tool return reaches the model as its repr; an MCP result arrives as content blocks; the adapter puts structured content under its artifact key; the adapter has no per-call timeout | keep: restart conditions | Holds |
| A FastMCP tool is a mutable object; a client session exposes `_request_id`; a read-bound timeout is a 408 | keep: the `mcp` SDK, capped `<2`; each reaches a private name and is a candidate for an `mcp` issue | Holds on 1.30.0 |
| The pinned versions are the ones the assertions were measured against | keep: a floor | Raise with the lockfile (W5.2) |
| PyJWT fetches its key set through `fetch_data`; fills its cache; refetches at once with its own cooldown off | keep: third party (`pyjwt` 2.15.1, latest) | Holds |
| RDKit's fragment catalogue; psycopg's connect and pool absorb no cancellation; a pipeline block is one transaction; `httpx-sse`'s decoder | keep: third party (`rdkit` 2026.3.5 against 2026.9.1; `psycopg` 3.3.6 and `psycopg-pool` 3.3.3, latest; `httpx-sse` 0.4.3, latest) | Holds on the installed versions; not bumped here |

## Upstream text the model reads

First-party text is unchanged by the bump: the system prompt of all seven profiles compiled with a
scripted model is byte-identical (after the per-process envelope nonce), as are the schemas of all
75 first-party tools. What changes is text upstream owns, and a bump to 0.7.25 ships all of it:

- `read_file` and `edit_file` tool descriptions. `read_file` output stops carrying a line-number
  gutter and starts with a status header (`@@ field | field | … @@`); the descriptions say so.
  The model reads skills with `read_file`.
- The `task` tool description loses "in an isolated context window".
- The text that answers a tool call dangling after a crash ("did not complete - no result was
  recorded. It may have been cancelled or interrupted.", now with `status="error"`), and the text
  `create_agent` answers a malformed call with.
- The preview shown in place of an offloaded oversized tool result, and the `glob`/`ls` empty and
  truncated notes.
- `task` rejects unknown argument keys instead of dropping them.

`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation` applies to these as to any
text: they are not first-party, which is why the bump cannot hold them still, and they need the
evaluation that needs the model. `schema/model-text/inventory.json` is regenerated with the bump so
the diff is visible. The bump is a lockfile revert if the evaluation says no.

## Upstream issue drafts

Seven drafts, each with a minimal reproduction run against the latest release, in `upstream-issues/`.
They cannot be filed from the session that wrote them; filing is a row in `BACKLOG.md`.

1. [langgraph: `durability="sync"` in a subgraph with no checkpointer](upstream-issues/01-langgraph-sync-durability-without-a-checkpointer.md)
2. [deepagents: `permissions=` does not reach a replaced `FilesystemMiddleware`](upstream-issues/02-deepagents-permissions-do-not-reach-a-replaced-filesystem-middleware.md) — fixed upstream in 0.7.31; not to be filed
3. [deepagents: a `HarnessProfile` per agent](upstream-issues/03-deepagents-a-harness-profile-per-agent.md)
4. [deepagents: `SkillsMiddleware` run-scoped listing, empty-listing text and an unconditional hook](upstream-issues/04-deepagents-skills-middleware-run-scoped-listing-and-hooks.md)
5. [langchain: context editing runs on the event loop](upstream-issues/05-langchain-context-editing-runs-on-the-event-loop.md)
6. [langchain: `ModelCallLimitMiddleware` is skipped by an `after_model` jump](upstream-issues/06-langchain-model-call-limit-is-skipped-by-an-after-model-jump.md)
7. [deepagents: `import deepagents` imports three provider SDKs](upstream-issues/07-deepagents-import-pulls-in-three-provider-sdks.md)

## Refreshing this document

1. `uv lock --upgrade-package deepagents --upgrade-package langchain --upgrade-package langgraph …
   --dry-run` lists what moves; the PyPI JSON API gives the latest of anything not in the lock.
2. Run `tests/test_upstream_surface.py` against the new lock. A red pin is a row to re-read, never a
   value to update.
3. Compile the production agent for every profile with a scripted model and diff the system message
   and the bound tool schemas against the old lock; diff upstream's own strings the same way.
4. Re-run each reproduction in `upstream-issues/` against the latest release; an item whose
   reproduction passes is closed, and its shim, its pin and its draft go in one commit.
