# D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use — Temporal's LangGraph plugin is declined

**Status:** accepted · **Date:** 2026-08-25 · Does not supersede anything. It answers the spike
`docs/planning/BACKLOG.md § 5` opened ("Two durability layers are maintained where upstream now
ships one") and closes that row with a **no**, on evidence the row itself did not have.

## Context

The 2026-08-25 field benchmark ranked this fourth of eight findings. The argument was clean: Temporal
shipped a LangGraph plugin in public preview, this repository hand-built the equivalent
(`agent/checkpointer.py` on its own autocommit pool, `agent/plan_approval_store.py`, the job→session
push-back), and it carries two open defects in exactly that seam plus a queued row asking for the
durable approval store the plugin appears to make free. Maintaining two durability layers where
upstream ships one is a real cost, and the row was right to ask.

The spike was timeboxed to five days and asked three questions. It took rather less than that,
because the first answer settles the other two.

## What the spike found

**The plugin is installed already.** `temporalio.contrib.langgraph` ships in `temporalio` 1.31.0,
which this repository already depends on — so the version bar the row worried about was never the
obstacle. (A first check looked for `temporalio.contrib.langchain` and concluded it was absent. It
is `langgraph`. Recorded because the wrong module name is exactly how a spike reaches a confident
wrong answer.)

**Question 1 — does `interrupt()`-as-signal close *"A decided approval hold can be reopened"*?**

**No, and it cannot, because this system does not use `interrupt()`.** `grep -rn "interrupt(" src/`
returns two hits and both are prose in `agent/checkpointer.py` explaining that a checkpointer would
be needed *if* one were used. The human-in-the-loop path here is not a graph interrupt at all:
`agent/interaction_tools.py::start_approval` calls `client.start_workflow` directly. The hold is
*already a Temporal workflow*.

So the plugin's headline feature — a durable pause with no process held open — is a thing this
repository already has, arrived at from the other direction. And the defect is not about durability:
it is that the workflow is started with no `id_reuse_policy`, so a decided hold can be started again
under the same id. The row already names the fix and why the two obvious policies are wrong (expiry
deliberately *completes*, so `REJECT_DUPLICATE` and `ALLOW_DUPLICATE_FAILED_ONLY` both make an
expired candidate unofferable forever while its button still renders). That fix is a few lines
against `start_approval` and is entirely independent of this plugin.

**Question 2 — does it close the durable approval store row?** No, for the same reason. That store
exists because an approval must not outlive, or be outlived by, the mode it authorises — and
`plan_approval_store.py` already follows the session store's backend precisely so the two lifetimes
match. The plugin makes *graph node execution* durable within a workflow run; it says nothing about
where a human decision is recorded or when it is spent.

**Question 3 — what would it cost against the checkpointer's three measured reasons?** The question
is moot given the first two answers, but the shape is worth recording because it decides any future
revisit.

The plugin takes `graphs: dict[str, StateGraph]` and requires **each node's `metadata` to carry
`execute_in`** (`"activity"` or `"workflow"`) plus its activity options. Measured against a real
compiled agent: `build_langgraph_agent` returns a `CompiledStateGraph` whose `.builder` is a
`StateGraph`, so the input shape is reachable — and every one of its nodes has `metadata=None`:

```
nodes: model, tools, SummarizationMiddleware.before_model,
       PatchToolCallsMiddleware.before_agent, ReloadingSkillsMiddleware.before_agent
```

Those nodes are built by `create_deep_agent`, not by this repository. Annotating them means mutating
somebody else's builder after construction, keyed on node names upstream never promised — which is
precisely the coupling `tests/test_upstream_surface.py` exists to count, and it would be the widest
one in the file.

## Decision

**Declined.** The two durability layers stay.

Three reasons, in the order that decides it:

1. **It solves a problem this system does not have.** The plugin's value is durable `interrupt()`,
   and there is no `interrupt()` here. The human gate is already a Temporal workflow.
2. **It closes neither defect the row attributed to it**, and both fixes are small, local and
   available today.
3. **It would cross `D-2026-08-10 §3`.** That line says Temporal keeps every long or expensive job
   and layer 1's checkpointer holds turn state and nothing else. Running every model call as a
   Temporal activity is a much larger change than "durable interrupts", and it would have to be
   argued as one rather than adopted as a maintenance saving.

A fourth, which is not load-bearing but is true: the plugin's own docstring says *"This package is
experimental and may change in future versions. Use with caution in production environments."*

## Consequences

- **The backlog row is deleted** rather than left open, because the question it asked is answered.
  The two defects it bundled stay as their own rows and are unblocked — neither was waiting on this.
- **The restart condition, stated so a future reader does not re-derive this.** Revisit if any of
  these becomes true: this system adopts `interrupt()` for a human gate (which would mean moving the
  approval hold *out* of Temporal and into the graph — itself a decision needing an ADR); or
  `create_deep_agent` gains a supported way to set node metadata, removing the coupling in question
  3; or the plugin leaves experimental *and* the graph is authored here rather than upstream.
- **`tests/test_upstream_surface.py` gains nothing from this.** There is no absence to assert: this
  is a decision not to adopt an available thing, not a workaround for a defect that upstream might
  fix. The register that *should* carry it is the upstream-capability section in
  `docs/planning/BACKLOG.md`, which is where a "what does upstream now ship that we build ourselves"
  question belongs and where this ADR is cited.
- **The benchmark's finding 4 stands as a question and falls as a recommendation**, and the review
  is left as written. A point-in-time review is a record of what was believed on its date; this is
  the ADR that checked it.
