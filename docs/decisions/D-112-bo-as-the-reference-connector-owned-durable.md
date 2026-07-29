# D-112 — `bo` as the reference connector-owned durable capability

The `bo` bundle is the one that proves the durable half of the seam rather than describing it. It
owns both flavours: `suggest_next_experiment` is an inline MCP tool on its own FastAPI server, and
the campaign is a `jobs:` entry whose workflow, activities and **worker** all live in the bundle,
polling `connector-bo`. Core's background worker no longer serves any BO workflow or activity, and
`agents/durable_tools.py`'s bespoke campaign launcher is deleted — the manifest replaced it.

**What this establishes.** Moving a durable workflow out of core was one manifest entry plus changing
the workflow's return type to `ConnectorJobResult`. Nothing in core was edited to accommodate it,
because `ConnectorJobWorkflow` addresses the child by workflow *type name* and task queue, both
strings from `connector.yaml` — the property D-110 claimed and this is the first exercise of. The
practical payoff is that `bofire`/`botorch` now load only in the bundle's two processes.

**The PR-gate split, made structural.** `write_campaign_node` — an activity that both *built* the
recommendation note and *published* it — is gone. The mapping (BO result → note) stayed in the
bundle, because that is the domain's knowledge; the publish moved to core, because the PR-gate is the
GxP boundary. A connector now returns a note in its envelope and cannot reach the gate at all, which
is a stronger statement than "it is not supposed to".

**One new manifest field, and why it earns its place with a single caller.** The deleted adapter
enforced `require_rounds_within_ceiling` before starting: a campaign re-sends its whole observation
history each round, so history grows quadratically and past the ceiling Temporal terminates the run
mid-flight, losing every already-paid evaluation. Migrating the job would have dropped that guard
silently. Every other placement is replay-unsafe — a validator on `CampaignSpec` or a check inside
the workflow re-runs during replay against *current* config, so lowering the ceiling would
retroactively fail an in-flight campaign that was legal when it started. The launch boundary is the
only safe place, and after the factory replaced the hand-written adapters that boundary is the
generated tool. So `JobSpec.precondition` names a `module:function` the factory calls before any
durable work. It has one caller today and is not speculative: without it, migrating a job to the
generic path is a silent regression, which is the opposite of what the seam is for.

Remaining in Stage C: the `kg` bundle, and the `qm`/`report` jobs, which follow the same shape once
their workflows move and return the envelope directly.
