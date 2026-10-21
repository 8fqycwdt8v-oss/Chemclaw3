# D-2026-10-21-a-tool-group-is-shown-to-the-model-when-the-turn-needs-it — the graph holds every eligible tool; the model sees the eager ones plus the groups the turn activated

**Status:** proposed · **Date:** 2026-10-21

## Context

Every bound tool schema precedes the system message on every model call. `tests/test_context_floor.py`
caps that prefix at `CEILINGS["__default__"] = 73_450` tokens (`PREFIX_BOUND = 83_700` with the
fleet allowance), and `core/config/agent.py` derives both compaction thresholds from it. To stay
under it, five process-development bundles and `pyexec` ship `default_enabled: false`
(D-2026-09-20). That makes them unreachable rather than cheap. Adding source query tools
(D-2026-10-21-a-data-source-contributes-its-own-surface) would grow the prefix again. LangGraph's
`ToolNode` can run only the tools it was built with, and there is no on-demand binding today.

## Options

1. **Keep static binding.** Capability keeps trading against context, one `default_enabled` flag
   at a time.
2. **Recompile per activation.** Rebuild the graph mid-turn when a capability is asked for. It
   fights W5.4 (compile once per process), and a mid-turn rebuild loses the checkpointed tool
   state.
3. **Compile the eligible surface; filter what the model sees.** A `wrap_model_call` middleware
   sets `request.tools` to the eager tools plus those of groups activated in checkpointed state.
   A `use_capability` tool, or reading a skill that names a group's tools, activates a group.
   Execution, audit and authorization are unchanged because the `ToolNode` holds everything
   eligible.
4. **A router model picking tools per turn.** "A specialist router" is listed as not in this
   system, and it adds a model call to every turn.

## Decision

**Option 3.**
- **State.** `ChemclawState.active_capabilities` is checkpointed and capped at
  `agent_active_capability_max` (default 3), evicting the least recently used group.
- **Order.** Tools are sent in a stable order: eager first, then groups in activation order. The
  cached prefix therefore breaks once per activation, not once per turn.
- **Inactive groups.** A call to a tool of an inactive group is refused with a message naming
  `use_capability`.

## Consequences

- `CEILINGS` becomes the eager floor. `PREFIX_BOUND` becomes eager floor plus
  `agent_active_capability_max × ON_DEMAND_GROUP_MAX_TOKENS` plus allowances, measured into `data/`
  under W5.7.
- `bind: on_demand` replaces the binding meaning of `default_enabled`, which keeps only "declared
  but not enabled".
- The process-development bundles, `pyexec`, `review_commitments` and source query tools become
  on-demand groups, and the eager `CEILINGS` is ratcheted down to the new measurement.
- Ships behind the model-text evaluation of D-2026-10-08; a group returns to `eager` if its
  activation measurably hurts answers.
- Plan item: W10 Track C. Depends on W5.4 and W5.7.
