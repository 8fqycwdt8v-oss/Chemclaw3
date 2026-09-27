-- A human's standing approval for the durable jobs one composed workflow may launch
-- (D-2026-09-15-an-approval-is-for-one-version-of-one-workflow).
--
-- Migration 100 made a workflow the agent composed runnable, and
-- `D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction` refused a `job` step in
-- one — naming the cost in the same breath, because every durable job launcher is state-changing
-- and so the rankings and conformer searches these procedures exist to sequence were exactly what
-- an agent-authored one could not contain. These three columns are the widening, and what makes it
-- a widening of the control rather than a hole in it is *what* is approved.
--
-- **The approval is for one version of one workflow.** `approved_fingerprint` holds
-- `durable/template_job.template_fingerprint(document)` — the same hash a resumed run compares, a
-- hash of the whole resolved template. The alternative, and the phrase this feature was asked for
-- in, was a standing approval *per actor*: "this chemist's composed workflows may launch jobs".
-- That never lapses, so a workflow re-composed into something else inherits the permission granted
-- to what it used to be, which is the one failure mode worth designing against here. Keyed on the
-- document's own hash, **re-composing lapses the approval automatically** — the stored fingerprint
-- simply stops matching, with no clearing logic anybody can forget to run. That is `plan_approvals`'
-- shape one layer up, where a rewritten plan is a different key.
--
-- `approved_by` is a person, never the agent. Nothing the model can call writes these columns:
-- the only writer is `POST /workflows/{name}/approval`, a front-door route, for the reason
-- `api/routes/plan.py::decide_plan` gives for being a route and not a tool — a model must never be
-- able to authorize its own plan. `tests/test_composed_workflows.py` asserts the absence rather
-- than trusting it.
--
-- Additive and defaulted, so an existing row reads as unapproved: the empty string matches no
-- fingerprint, which is the correct reading of "nobody has decided about this yet".

ALTER TABLE composed_workflows
    ADD COLUMN IF NOT EXISTS approved_fingerprint TEXT NOT NULL DEFAULT '';

ALTER TABLE composed_workflows
    ADD COLUMN IF NOT EXISTS approved_by TEXT NOT NULL DEFAULT '';

ALTER TABLE composed_workflows
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
