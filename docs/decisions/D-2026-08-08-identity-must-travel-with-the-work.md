# D-2026-08-08-identity-must-travel-with-the-work — a role name is not an entitlement

**Status:** accepted

## Context

Four identity defects from the same review, sharing one shape: **an identity that was resolved and
then not carried to the place that needed it** — or, in the first case, carried from a place that
had no business supplying it.

**1. Skill visibility conferred tool authorization.** `cli/chat.py`'s `resolve_identity` gave
`--admin` the union of every role named in `settings.skill_role_gates`. That map decides which
*skills a chemist is shown*. `authorize_tool` reads `tool_role_gates` and `entra_privileged_role_set`;
`authorize_trigger` reads the latter. Unrelated maps, coupled by nothing but the role *name*.

On the shipped chart the derivation is harmless — measured, 36 tools allowed and 6 denied, 0 of 5
expensive actions. The hazard is that both halves are things an operator is told to do. The
skill-gate example in `core/config/agent.py` is `{"deep-research": ["process-chemist"]}`; the
runbook's remedy for a refused expensive job is to put a role in `entra_privileged_roles`. Do both,
in two edits neither of which mentions the CLI, and:

```
roles=['process-chemist']   42 of 42 tools allowed, 0 denied
                            5 of 5 expensive actions allowed
                            require_actor() -> 'admin@localhost'
```

`uv sync` installs the `chemclaw` console script into the image, so that is anyone who can `oc exec`
into a running pod, holding no token at all.

**2. Durable-job note proposals were recorded with no actor.** `propose_note` records a durable
`NoteProposal` whose `actor` comes from `ambient_provenance()`, and `set_current_identity` appeared
in exactly one file under `durable/`. So every note a durable job PR-gated was stored with
`actor=""` — while `ConnectorJobInput.requested_by` sat one frame above, required and unused.
`list_note_proposals` scopes a non-reviewer's queue to `principal.oid` and `_visible_proposal` 404s
the detail view, so the chemist who launched the job could not see the PR opened on their behalf.
That surface's own docstring gives exactly that as the reason it exists.

**3. The report workflow carried no requester at all.** `ReportRequest` had `title` and `sections`
and nothing else, while every other user-launched job input has a `min_length=1` `requested_by` —
and `request_development_report` called `require_actor()` and **discarded the result**. In an
activity with no identity, `ShareDocumentRetriever._entitled()` correctly declines and returns `[]`
without reaching the index; `gather_section` only concatenates, so an un-entitled source is
indistinguishable from one with no matches and `retrieval_failed` stays False. A chemist holding the
share's role received a draft that read as a complete sweep of every internal source, with the share
skipped and nothing saying so.

This also corrects `docs/planning/BACKLOG.md`, which proposed propagating "the report's
`requested_by` (which the workflow already carries)". It carried no such thing.

**4. `/approve` recorded the wrong person.** `_plan_command` hardcoded `settings.cli_admin_actor`
while the session ran under `--actor`, so the durable record of a GxP sign-off named an identity
that took no action and disagreed with the audit rows for its own session.

## Decision

**An identity is resolved once, at the boundary, and travels on the work — and only a map that
means "entitlement" may confer one.**

- `settings.cli_admin_roles`, **empty by default**, replaces the `skill_role_gates` derivation.
  `--admin` bypasses *authentication*; authorization still applies, and the docstrings that said
  otherwise are corrected. A deployment wanting a full-access local seam populates it deliberately —
  a decision someone makes, rather than a consequence of naming two unrelated things alike.
- `publish_memory_note_activity(note, actor="")` stamps the ambient identity for the duration of the
  gate, and `ConnectorJobWorkflow` passes `job.requested_by`.
- `ReportRequest` gains `requested_by` and `requested_roles`, populated from the `require_actor()`
  that was already being called, and a `SectionRequest` carries both to the child workflow.
- `_plan_command` takes the run's actor.

**On the "absent" default.** `publish_memory_note_activity`'s `actor=""` is real and stays: the
memory-synthesis jobs are schedule-triggered, have no user, and a synthetic actor would make an
unattributed proposal look attributed — worse than an honestly empty one.

`ReportRequest.requested_by` is **not** such a case, and the first version of this ADR claimed it
was. There is no scheduled-report launcher: `request_development_report` is the only constructor in
`src/`, and `require_actor()` either raises or returns `settings.service_actor_id`, never `""`. The
optional field bought a branch nothing could reach and a test that proved nothing about production,
while permitting a future caller to launch a report with no attribution. It is `min_length=1` now,
matching `ConnectorJobInput` and `TemplateRunInput`.

**On roles captured rather than looked up.** `requested_roles` is snapshotted at launch because
there is nothing to look an actor's roles up *in* — the front door reads them from the validated
token into a contextvar for the turn, and a background run has no turn. If they do not travel on the
request they do not exist by the time an entitlement is checked.

**On stamping a requester's roles onto a background run**, which the backlog row correctly flagged
as needing a decision: it widens what that run can read, and that is the right trade *here* and not
in general. The sections are the requester's own question, the draft is proposed for them, and the
alternative on offer was not "read less" — it was "read less and say nothing about it". The
corollary is the run id below: a run that reads one chemist's corpus may not be shared with another.

**On the report's run id.** Making `retrieve_section` read entitlement-gated sources as the
requester changed what `_report_id` means. It keyed on title and sections only, which was sound
while a report read the same corpus for everyone — and the moment it did not, sharing a run became a
cross-user exposure: Alice with the share role launches, the gated documents land in the draft, Bob
asks for the same title and sections, `WorkflowAlreadyStartedError` hands him the same id, and
`job_status()` applies no actor check at all (`find_past_jobs` explicitly gives people other
chemists' job ids for that call). The id now includes the requester and their sorted roles. Two
chemists with the *same* entitlement still share a run, which is where the idempotency argument was
true all along.

This was caught by review, after the first version of this change shipped the exposure. Worth
recording as a shape rather than an incident: **widening what a job reads changes what its
deduplication key has to mean**, and the key lived in a different file from the change.

## Consequences

`--admin` is now what its name says. The measured configuration that opened every tool and every
expensive action confers nothing, and
`test_a_skill_visibility_gate_cannot_confer_tool_authorization` pins the two maps apart using that
exact configuration.

A chemist can find the PR a job opened on their behalf, and a report drafted for someone who holds a
gated source's role actually reads it.

The report identity is asserted **at the activity**, not at the workflow, because that is the only
place it has to be true: a value that reaches the workflow and stops there is precisely the defect
being fixed. Its counterweight — a fan-out payload with no requester stamping nothing — is pinned in
the same file, so a future change cannot satisfy one by breaking the other.

`ReportSectionWorkflow` and `retrieve_section` changed their argument type from `ReportSection` to
`SectionRequest`, which **a report in flight across the deploy cannot replay**: the old payload
fails validation, the workflow task retries forever and the parent's fan-out never completes. Drain
report workflows before upgrading, or accept that any in-flight report must be cancelled and
re-requested. Stated here because a breaking change that is only discovered in the cluster is the
same defect as an unstated one.

What this does not do is give the durable layer a general identity seam. Four call sites now carry
an actor explicitly; a fifth that forgets will fail the same way, silently. The general fix is a
`requested_by` on the durable-job envelope itself rather than on each input model, which is worth
doing when a fifth appears and is not worth inventing for four.
