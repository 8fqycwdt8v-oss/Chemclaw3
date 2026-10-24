# deepagents: `SkillsMiddleware` cannot declare a run-scoped listing, and always adds a model hook

**File against:** `langchain-ai/deepagents`. **Status:** parts 1–3 filed as [deepagents#7237](https://github.com/langchain-ai/deepagents/issues/7237) (2026-10-24); part 4 is fixed upstream (0.7.32 no longer matches `PrivateStateAttr` by identity). **Observed on:**
deepagents 0.7.25 (latest on PyPI, 2026-10-14) and 0.7.8 (installed here). Four independent requests;
file them separately if upstream prefers.

## 1. A listing that depends on who is asking has to be reset by the caller on every run

`SkillsMiddleware` loads the listing once per thread and caches it in `skills_metadata`. Since 0.7.16
a caller can request a reload by writing `skills_metadata = None` into the run's input or into the
thread with `update_state`. That covers a skills tree that changed on disk. It does not cover a
listing that depends on the caller's identity (a role gate evaluated per request): every entry point
that starts a run (an HTTP turn, a resume, a batch step) has to remember to pass `None`, and the one
that forgets serves one caller another caller's listing from the checkpoint.

Proposed: a constructor option such as `SkillsMiddleware(..., reload="every_run")` that treats the
cache as run-scoped, so the guarantee is a property of the middleware and not of its callers.

First-party workaround: `src/chemclaw/agent/langgraph_agent.py::ReloadingSkillsState` redeclares
`skills_metadata` as an `UntrackedValue` channel, which a checkpoint never stores, so the key is
absent at the start of every run and upstream's own `state.get("skills_metadata") is not None`
short-circuit never fires. The redeclaration depends on `PrivateStateAttr`, which is importable from
`langchain.agents.middleware.types` but not in its `__all__`.

## 2. The empty-listing text cannot be set

`SkillsMiddleware._format_skills_list` returns a sentence inviting the model to create a skill when
nothing is listed. A deployment where every skills tree is read-only to the model has to override the
underscore-prefixed method to say something true.

Proposed: a constructor parameter for the empty-listing text (or make the method public).

First-party workaround: `ReloadingSkillsMiddleware._format_skills_list` returns `NO_SKILLS` when the
list is empty and defers to `super()` otherwise. A related edit, removing passages of
`SKILLS_SYSTEM_PROMPT` that are false on this deployment (script execution, example skill names), is
done by substring on the upstream constant and passed through the public `system_prompt=` parameter;
a way to compose the template from its parts would remove that fragility.

## 3. The middleware registers `before_model` even when `pinned_skills` is never used

Since 0.7.23 `SkillsMiddleware` defines `before_model`/`abefore_model` (it appends the skills named in
`pinned_skills`). `create_agent` gives every hook its own graph node, so each agent gains one node and
therefore one superstep per model call whether or not anything is ever pinned. With a checkpointer
that is one more checkpoint write per model call.

Proposed: register the hook only when the feature is enabled (a constructor flag or an
`AgentMiddleware` that is composed in only for callers who pin).

First-party observation: compiling the production agent on 0.7.25 adds a
`ReloadingSkillsMiddleware.before_model` node to every profile's graph; none of this repository's
code writes `pinned_skills`. A scripted turn with an in-memory saver writes 11, 18 and 39
checkpoints for 1, 2 and 5 model calls on 0.7.8 and 12, 20 and 44 on 0.7.25 (one more per model
call, and one for the turn).

## 4. `PrivateStateAttr` is matched by identity and is not exported

`deepagents.middleware._state.private_state_field_names` decides which state keys stay out of a
subagent with `any(meta is marker for meta in args[1:])`, where the marker is
`langchain.agents.middleware.types.PrivateStateAttr`. That name is not in the module's `__all__`,
and its documented equal, `OmitFromSchema(input=True, output=True)` (which is exported), compares
equal but is a different object, so a state schema written against the exported class silently
leaves its keys crossing the `task` boundary in both directions.

Proposed: compare with `==`, or export `PrivateStateAttr`.

First-party handling: `agent/state.py` and `ReloadingSkillsState` import `PrivateStateAttr` from the
module; `tests/test_state_channels.py` asks `private_state_field_names` which channels are private
and drives each, so a marker `deepagents` stops recognising turns it red.

## What turns red when this is fixed

- `tests/test_upstream_surface.py::test_the_skills_middleware_still_caches_under_skills_metadata`
  and `::test_the_skills_middleware_still_formats_its_listing_under_a_private_name` pin the names the
  workaround redeclares and overrides.
- `tests/test_middleware_order.py` records the compiled node list; the extra node is visible there.
