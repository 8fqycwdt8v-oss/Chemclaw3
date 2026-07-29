# D-116 — Seventh reconciliation with `main` (PR #30): two capabilities the merge silently restored

The e2e-testing branch merged into `main` while this one was open, and the reconciliation is
mechanical in the direction that matters — `main` does not have the connector seam, so every
conflict where it re-introduces `settings.mcp_servers`, `agents/calc_tools.py`, `agents/bo_tools.py`
or `workflows/bo_campaign.py` resolves to this branch. What is worth recording is the two places
where "resolve to ours" was the *wrong* answer, and the class of defect that produced them.

**A merge that deletes a file on one side and edits it on the other restores the file.** Git reports
that as `modify/delete` and asks; it does not report the *transitive* case, where the deleted file's
module is still imported. Four modules came back this way — `workflows/xtb_job.py`,
`workflows/xtb_activities.py`, `agents/xtb_job_tools.py`, `agents/xtb_expert_tools.py` — all replaced
by the `calc` bundle's durable half in D-114, none flagged as a conflict, because on this branch they
simply no longer existed. Two of them were dead-but-harmless; `xtb_job_tools.py` imported a module
this branch had deleted, so it was an `ImportError` waiting for the first test that touched it.

**The one that would have been a real regression.** `connectors/bo/activities.py` came back carrying
`@durable_activity("background")`. Git's rename detection had matched it to `main`'s
`workflows/bo_activities.py`, which legitimately registers on core's queue — so the decorator
followed the file across the boundary. The effect: core's background worker would have served the BO
activities again, loading `bofire` and `botorch` into the process the bundle exists to keep them out
of. Nothing about it looks wrong in a diff; it is three decorator lines in a file whose contents are
otherwise correct.

`tests/test_workflow_registry.py` caught it, and how it caught it is the lesson. `main`'s
`test_every_declared_capability_reaches_its_worker` asserts `BACKGROUND_WORKFLOWS ==
registered_workflows("background")` — a snapshot taken at worker import compared against the live
registry. A capability registering *after* that snapshot makes the two disagree, which is exactly
what a stray connector registration does. The absence-assertion added in D-114 covers the same
boundary from the other side; between them the failure is now caught twice, and the docstring in
`connectors/bo/activities.py` says why the decorator must not be there.

**Adopted from PR #30, each verified present after resolution rather than assumed:** the two
error-surfacing middlewares (`surface_authorization_denials`, `surface_domain_errors`) around audit
and authz — the chain is four deep now, not two; the BO argument coercion, which this branch had to
*port* into `connectors/bo/server/tools.py` because that file is a rename of the module the fix
landed in, so the merge kept this branch's older body (a plain "ours" resolution would have dropped a
live-e2e finding: the model sometimes JSON-encodes the observations array as a string); the report's
retrievers coming from `sources.registry.active_retrieve_sources()` rather than a hardcoded
`GraphRetriever()`; `find_notes` matching every query word independently; the xTB fixed-point fix;
and the registry's re-import safety.

**Two of this branch's own tests were wrong in the same way, and it is worth naming.** Both asserted
a *count* where they meant a *property*: the middleware chain "has length 2", and an authorization
message contained one specific phrase. Both broke on additions that were improvements. They now
assert what they meant — the narrowed agent's chain equals the default agent's (by name, since the
audit entry is a per-agent closure), and the denial names the actor and the tool. A test that pins an
incidental number is a test that will one day block a good change and teach nobody anything.

**ADR numbering, per the ledger rule `main` added in the interim.** That rule says the branch merging
second renumbers; this is that branch. `main` had taken D-109, so this branch's six ADRs moved from
D-109…D-114 to D-110…D-115, with every in-repo reference updated, and all seven numbers are now
reserved in `ADR-REGISTRY.md` — which is the mechanism that should make this the last renumber.
