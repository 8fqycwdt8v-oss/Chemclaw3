# D-143 — Nobody was collecting the metrics, and the durable history is never compacted

**Status:** accepted · **Context:** REV-2 and REV-4, the two remaining High findings. One is a
four-line chart file that should have existed from the start; the other is a confirmed defect whose
obvious fix destroys data, so it is documented and pinned rather than patched.

### Nothing scraped `/metrics` (REV-2)

The route has existed since DEP-4. Nothing under `deploy/` collected it — no ServiceMonitor, no
PodMonitor, no `prometheus.io/scrape` annotation. Every counter, gauge and histogram in the system
was exposed and uncollected in production.

That is the quiet way an observability story fails: the code is written, the endpoint answers, and
no dashboard or alert has ever had a data point. It is also the finding that retroactively blunts
several others — `chemclaw_connectors_unreachable_total` (D-139), `chemclaw_notes_proposed_total`
and `chemclaw_jobs_started_total` (D-139), `chemclaw_rollback_watermark_unavailable_total` all exist
specifically so an operator can *see* something, and none of them was reaching anyone.

**Decision:** a `ServiceMonitor` on the front-door Service, gated on `monitoring.enabled`.

- **A ServiceMonitor, not annotations**, because the target is OpenShift, whose user-workload
  monitoring stack is the Prometheus Operator; annotations are the older convention its default
  configuration does not read.
- **By port *name*** (`http`), so a port change cannot silently orphan the scrape.
- **The front door only.** The workers and connector pods import `chemclaw.metrics_bridge`, whose
  entire contract is that recording a metric outside the front door is a no-op — there is no
  registry and no HTTP surface in those processes. A scrape pointed at them would collect nothing
  and report the target as healthy.
- **`additionalLabels: {}`**, because a cluster's `serviceMonitorSelector` is release-specific and
  cannot be guessed; an operator sets it, and an empty default is honest about not knowing.

`ServiceMonitor` joins `Route` in `_UNVALIDATED_KINDS` — the chart's existing guard caught the new
CRD immediately, which is what that guard is for. And the path is checked against the *app*: a
ServiceMonitor naming `/metric` renders, validates, deploys, and collects nothing forever while
Prometheus reports the target down and an operator reads it as a broken pod. That is D-142's lesson
applied — a production value has to be executed, not type-checked.

### After-run compaction does not apply to the durable store, and the obvious fix corrupts data (REV-4)

**Confirmed.** `CompactionProvider.after_run` reads
`session.state[history_source_id]["messages"]` — where `InMemoryHistoryProvider` keeps its thread.
`PostgresHistoryProvider` deliberately keeps nothing there, which is the entire point of it, so the
lookup finds nothing and the strategy returns having touched nothing. Under the production default,
`_build_compaction`'s `after_strategy` is a silent no-op, and its docstring's promise to "shrink the
persisted history so the next turn starts smaller" was false.

**Two corrections to how the finding was framed.** The `before_run` half *does* work under Postgres
— it compacts what earlier providers loaded into the context — so the model's input is still
bounded and this is not a context-window bug. What is unbounded is this provider's own read
(`_SELECT` has no `LIMIT`, so every turn loads the entire history) and the stored history, which
grows for the session's whole life.

**Decision: document and pin, do not patch.** The obvious fix is a `LIMIT` on the load, and it is
unsafe in a way that is easy to miss. `get_messages` repairs unmatched tool-call pairings on read —
correctly, because a `SIGKILL` between a tool call and its result leaves a genuine orphan that
breaks every later turn — and that repair **writes back**, deleting and rewriting stored rows. Over
a windowed read, a `tool_result` whose `tool_use` merely fell outside the window is indistinguishable
from one whose `tool_use` never arrived. The repair would strip it and commit that, permanently
destroying a pairing that was intact on disk.

So a correct bound needs either the repair to run in memory only when the load is partial, or real
durable compaction that prunes whole groups from `session_messages`. Both are design changes to a
durable path with a data-loss failure mode, and both want their own ADR rather than being written
under a review item.

What ships here is the honest version: both docstrings that promised the opposite are corrected, and
`tests/test_durable_compaction_gap.py` pins the no-op *and* the write-back hazard — the second
asserting that `_persist_repair` is still called, so the change that would make bounding safe is
also the change that turns that test red and forces the question to be asked. Pinning a trap is
worth more than a patch that hides it.
