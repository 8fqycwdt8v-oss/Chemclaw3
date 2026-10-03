# D-2026-10-03-an-artefact-push-expires-on-its-own-schedule — artefact push rows are pruned by a job of their own

**Status:** accepted · **Date:** 2026-10-03. Amends wave 2 of the artefacts contract ("`exhibit`
push rows are pruned by the retention sweep"), whose mechanism never ran on the deployments it was
written for.

## Context

A person's artefact write is pushed to the session's other tabs as an `exhibit` row on
`session_events`, and `exhibit_push_retention_hours` (default 24, deliberately *not* 0-disabled —
"an unbounded notification queue is not a retention policy anybody chose") was applied as the first
step of the retention sweep. Two defects, found in the deployability review and measured on
`durable/schedules.planned_schedules`:

1. The sweep is scheduled only under `retention_enabled` **and** a non-zero window. The shipped
   default states no window, so on every default deployment the push window pruned nothing, ever,
   and `session_events` kept every push.
2. The window predicate (`_retention_windows_are_set`) listed four `retention_*_days` settings while
   the sweep maps six. A release whose only stated window was `retention_session_exhibits_days`
   (or `retention_result_publications_days`) passed the chart's retention-posture gate and never
   scheduled the sweep.

## Decision

1. **The predicate is derived**: every `retention_*_days` setting is a window
   (`schedules.retention_window_fields`), and a test drives each one alone and holds the set against
   the sweep's own `_window_days` map, so a seventh window counts with no edit.
2. **Artefact pushes expire on their own schedule.** `prune_exhibit_pushes` /
   `ExhibitPushPruneWorkflow` (schedule `exhibit-pushes`) runs wherever a push can be written — the
   durable session store — every `exhibit_push_retention_hours`, whether or not a retention policy
   is stated, so a push outlives its window by at most one more. The retention sweep no longer
   prunes pushes; every other `session_events` kind keeps its `consumed_at` rule there.

## Options considered

- **Schedule the retention sweep whenever pushes can exist.** Declined: the sweep applies every day
  window it finds, so a deployment that set windows with `retention_enabled` off would start
  deleting conversation history it had not switched on — the one outcome the retention schedule's
  gate exists to prevent. Gating the windows on `retention_enabled` inside the sweep instead changes
  what a manual run of the sweep does and every test that drives it.
  Revisit when: the retention sweep gains a scope argument a Schedule can pass, so one workflow can
  run "pushes only" — the duplication this record accepts (two workflows over one `_prune_by_age`)
  is then a parameter.
- **Prune inline, on the write or the claim of a push.** Declined: it bounds the pushes of sessions
  that are still written to or read, and a session nobody opens again keeps its last pushes for
  ever — the same unbounded tail, smaller.
- **Make the push window 0-disabled like the day windows.** Declined: a notification queue with no
  bound is not a policy, and the list route is the source of truth whatever is pruned.

## What keeps it true

- `tests/test_schedules.py::test_every_retention_window_turns_the_sweep_on` and
  `::test_artefact_pushes_expire_whether_or_not_a_retention_policy_is_stated`.
- `tests/test_retention.py::test_an_artefact_push_goes_on_age_alone_and_nothing_else_does`, driven
  through `prune_exhibit_pushes`.
- `tests/test_workflow_registry.py` — the new workflow's park-rather-than-fail stance.
