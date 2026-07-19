"""Temporal worker processes that host workflow/activity code and poll a queue.

Workers are thin: they connect via `chemclaw.temporal_client` and register the
workflows/activities defined in `workflows/`. They hold no business logic —
restarting one mid-job is the durability spike at CHECKMATE 1.
"""
