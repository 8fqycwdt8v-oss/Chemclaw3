# D-2026-08-12-the-cap-was-right-and-what-it-was-holding-back — Lifting `deepagents<0.7`, and the write verb it was holding back

**Status:** accepted · **Date:** 2026-08-12

## Context

`pyproject.toml` capped `deepagents` below 0.7 with a stated condition: *"Lift the cap after reading
0.7's protocol, not before."* The reason was not general caution about a 0.x package. It was that
`agent/skill_backend.py` subclasses `deepagents.backends.FilesystemBackend` and narrows every reach
path through one predicate, so the protocol is a **security surface** here rather than an API — and
0.6.12's `backends/protocol.py` carried `removal="0.7.0"` markers announcing that surface would
change.

0.7.5 has now been read the only way that answers the question: both versions installed side by
side and diffed, file by file. Three changes touch this tree.

**1. The protocol gained `delete` — a write verb the gate did not refuse.** `NarrowedSkillsBackend`
refuses `write`, `edit` and `upload_files`; `delete` did not exist when that list was written, so a
bump alone would have inherited `FilesystemBackend.delete` working, into the one backend whose
entire reason to exist is that the skills tree is read-only. Unrefused it is worse than `write`, not
milder: a turn that cannot rewrite a `SKILL.md` but can remove it still decides what judgment the
*next* turn is able to load. `adelete` comes with it, and needs no separate override for the reason
the module already documents — the protocol implements it as `asyncio.to_thread(self.delete, …)`, so
it dispatches through the subclass.

**2. `grep` gained keyword-only `max_count` and `context_lines`, and upstream introspects for
them.** `protocol._method_accepts_max_count` decides whether to push the cap down to the backend or
apply it above — so an override that silently dropped the keyword would change how many matches a
caller gets depending on which class is underneath it.

**3. `FilesystemBackend.virtual_mode` now defaults to `True`**, which is what this repo already
passes explicitly, so nothing changes behaviourally. What changed is the *citation*: the deprecation
warning the module docstring quoted is gone with the old default.

**The cap was caught by a test, not by review, and that is the finding worth recording.**
`tests/test_skill_backend.py::test_every_reach_path_the_protocol_exposes_is_gated` derives the reach
surface from `dir(BackendProtocol)`, and on the bump it failed with
`['adelete', 'delete'] — gate them, or state why they cannot leak`. That is the test doing exactly
what its docstring promised. But the same run showed the derivation had a hole of its own: it
subtracted a **hand-written** set of write method names before checking the remainder, so a name in
that set was exempt from every assertion in the file. A method classified as a write and then never
called is indistinguishable from a method nobody thought about.

## Decision

**1. `deepagents>=0.7.5,<0.8`.** Up one major rather than uncapped: the argument for capping a 0.x
package whose protocol is a security surface is unchanged by having read one release of it.

**2. Refuse `delete` in `NarrowedSkillsBackend`,** beside the three refusals it joins.

**3. Forward `max_count` and `context_lines` from the `grep` override** rather than accepting and
dropping them. Filtering after the fact stays correct with a cap in play: `max_count` bounds what
the tree returns and this gate only ever removes from that.

**4. Derive the *classification*, not just the reach list.** The two tests now share one
`_WRITE_METHODS` set, and together they must account for every public method on the union of
`BackendProtocol` and `FilesystemBackend`:

- `test_the_skills_tree_is_read_only` calls **every** name in `_WRITE_METHODS` and requires a
  `PermissionError` from each — so adding a name there is no longer a way to exempt it, it is a way
  to sign up for an assertion. It also checks the file is still on disk afterwards, because a
  refusal that raises after acting is not a refusal.
- `test_every_method_the_backend_exposes_is_either_gated_or_refused` requires that the reach probes
  and `_WRITE_METHODS` *cover* the surface. An upstream addition must be triaged into one or the
  other before this file passes.

The union of both classes rather than the protocol alone: what a model can reach is what
`NarrowedSkillsBackend` inherits, so a public method added to `FilesystemBackend` only would be
invisible to a protocol-only derivation. The two surfaces are identical today — measured, `dir()`
difference is empty — which is a fact worth failing on rather than an assumption worth resting on.

**5. `ls_info`, `glob_info` and `grep_raw` probes are deleted, not kept.** 0.7 removed them from both
classes (they were the `removal="0.7.0"` markers), so the probes would raise `AttributeError` on a
method that no longer exists. A probe for an absent method proves nothing and reads as coverage.

## The measurement

Baseline on 0.6.12: `tests/test_skill_backend.py` — 11 passed, with deprecation warnings naming
`agrep_raw` as removed in 0.7.0.

On 0.7.5 before the fix, the derived test failed exactly as designed:

```
AssertionError: the protocol exposes reach path(s) this test does not probe:
['adelete', 'delete'] — gate them, or state why they cannot leak
```

After the fix, 12 passed. The refusal was then checked on the concrete class rather than inferred
from the override existing — `delete` and `adelete` both raise `PermissionError: the skills tree is
read-only`, and the `SKILL.md` is still on disk afterwards. Re-running the classification with
`delete` removed from `_WRITE_METHODS` reports `['adelete', 'delete']` as unclassified, which is the
check being load-bearing rather than decorative.

## Consequences

- **A second deviation closed itself on the bump, and it is named here because it was true of every
  turn the M12 routing run measured.** On 0.6.12, `SubAgentMiddleware.system_prompt` defaulted to
  `TASK_SYSTEM_PROMPT` — upstream's operating manual for the `task` tool: when to delegate, when not
  to, the spawn/run/return/reconcile lifecycle, and an instruction to parallelize. `agent/team.py`
  passes its own `_SUPERVISOR_PROMPT`, which **replaced** all of it, and which never names the `task`
  tool at all. In 0.7.5 `TASK_SYSTEM_PROMPT` does not exist: the guidance moved into
  `TASK_TOOL_DESCRIPTION`, which reaches the model through the tool schema regardless, and
  `system_prompt` defaults to `None` and is documented as *"Instructions appended to main agent's
  system prompt"* — which is exactly how this repo uses it. So the supervisor now gets upstream's
  mechanism half and Chemclaw's domain half, where before it got only the second. Nothing here
  changes `team.py`; the bump is the fix. **The archived M12 numbers were taken under the old
  behaviour** (supervisor delegated 0/15 on haiku, 1/15 on sonnet-5) and cannot be compared with a
  run taken after this commit.
- `agent_teams_enabled` stays `false`. This changes what a re-measurement would measure; it is not
  itself evidence about routing, and nothing here was run against a model.
- The module docstring's `virtual_mode` paragraph now says the argument rather than quoting a
  warning that no longer exists, and states why the explicit `virtual_mode=True` stays even though
  it is now the default: a security property that arrives as somebody else's default can leave the
  same way.
- The `langchain-google-genai` / `langsmith` closure note is unchanged and still holds. `uv sync`
  also moved `langchain-core` 1.5.3 → 1.5.4, `langchain-anthropic` 1.5.1 → 1.5.5 and `anthropic` to
  0.121.0 as transitive consequences.
- 0.7 ships middleware this repo does not adopt and this ADR does not open: `_prompt_caching`
  (D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it already answers that in the provider
  seam), `_message_eviction` and `_overflow_clip` (`agent/compaction.py` is the answered form of
  that question), and `permissions`. Each would need its own argument; none gets one by arriving in
  a dependency.
