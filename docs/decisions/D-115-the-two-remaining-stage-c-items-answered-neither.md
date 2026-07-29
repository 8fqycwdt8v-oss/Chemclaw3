# D-115 — The two remaining Stage C items, answered: neither becomes a bundle

Both open points closed by measuring rather than by preference, and the measurement says no in both
cases. Worth recording because "everything becomes a connector" is the wrong reading of D-110: a
capability earns a bundle by taking a dependency closure *with* it, and a tool that leaves the
closure behind gains nothing but a second code path.

**The `kg` bundle: won't build.** The open question was whether it would also own re-indexing. The
answer is that the question does not arise, because the graph is not a peripheral capability — it is
core's own data layer. Thirteen core modules import `kg`: the PR-gate, all six memory layers, the
report retrievers, the eval verifier, the note index. Moving `find_notes`, `expand_note` and
`find_knowledge_gaps` out would leave every one of those imports where it is, for a dependency win
of exactly zero, and add a second read path to one note tree. Re-indexing stays in core for the same
reason and one more: it is triggered by a merge into the note repo, which core owns. The rule is
written into `connectors/manifest.py`'s docstring and the runbook so the next author who notices
`find_notes` is not behind a connector finds the answer instead of re-deriving it.

**The `report` job: the envelope, not a bundle.** Its closure — the graph, the retrievers, the
embedding index — is what core keeps for `gather_evidence` regardless, so the isolation half buys
nothing. But the *uniformity* half turned out to matter: `DevelopmentReportWorkflow` returned a bare
note-ref string, which made the report the one durable job `get_durable_job_status` could report
`completed` for while having nothing to hand back. It now returns `ConnectorJobResult` and stays on
core's background worker.

It still publishes its own note rather than returning one for core to gate — correct here for
precisely the reason it would be wrong in a bundle. The note *reference* is the workflow's result, so
publishing is the work rather than a side effect, and this workflow already sits on the side of the
boundary the PR-gate lives on. A connector cannot make that claim, which is why D-112 took the
publish away from `bo`.

**What this leaves in core, as a closed list rather than a backlog:** conversation plumbing, the two
PR-gate writers, the knowledge-graph reads, `submit_qm_job` (it needs the HPC identity bridge, which
is core's), the report, and the two status tools. Every one of those is a rule with a reason, and
`tests/test_tool_registry.py` pins the set so adding to it is a reviewed edit.
