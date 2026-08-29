# D-2026-08-29-an-effect-declares-whether-it-can-be-undone — acting on a system we do not own

**Status:** accepted · **Date:** 2026-08-29 · Seventh of the eight infrastructure findings from the
2026-08-28 audit (F1). Answers the question
`D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question` left open.
**Corrects that audit's own framing of F1**, below.

## Context, and the correction

The audit reported that all three attachment seams refuse a write path, and concluded that a fourth
— an "effector" seam — was needed. **That framing was too strong, and the correction is the more
useful finding.**

`ConnectorManifest` has routed mutation through `jobs:` since D-029, and says so in its own
docstring: *"Mutation goes through a `jobs:` entry (which core authorizes, dry-run-gates and
attributes) or stays a core PR-gate tool."* The read-only rule the audit quoted applies to an
**endpoint's tool list**, not to a bundle. So a job could always write, and it already arrives with
idempotency (`job_workflow_id`), authorization (`expensive` → `authorize_trigger`), the plan gate
(`side_effecting_call`), the dry-run flag, an `audit_events` row and a `job_records` row.

What was actually missing is narrower and sharper:

1. **Nothing distinguished writing *our* database from writing somebody else's.** Re-running a
   cached calculation is free; filing a deviation twice is a second deviation. Both were "a job".
2. **Nothing declared reversibility**, so every job was gated identically whether it could be undone
   or not.
3. **Nothing recorded the external act** as a distinct, queryable fact. `job_records` says a run
   happened and what it returned, which is not the same as "this system changed something in the
   QMS and here is the ticket number".

Building a fourth seam would have added a manifest, a registry, a validator and a discovery path to
express something the existing seam already carries. The honest change is a **property on the job
declaration** — the same conclusion `D-2026-08-29-a-mirror-is-not-a-plan` reached one seam over.

## Decision

**`JobSpec.effect` — an `EffectSpec` naming the system reached and how the change can be undone.**

```yaml
jobs:
  - name: file_deviation
    workflow: FileDeviationWorkflow
    summary: open a deviation record
    expensive: true
    effect:
      system: the QMS
      reversal: irreversible
```

### `reversal` has no default, deliberately

`idempotent` (applying twice is applying once), `compensating` (undone by another declared job,
named in `compensation`), or `irreversible`. **No default**, because the safe-looking one is the
wrong one: a job whose author did not think about reversal is far likelier to be irreversible than
idempotent, and a default would let the un-thought-about case take the cheapest gate. A compensating
effect must name its compensation and no other kind may name one — both directions, because both are
a claim, and an unnamed compensation is a reversibility nobody can perform.

### An irreversible effect waits for a human, per call

This is the question D-2026-08-15 left open in as many words: `HumanInTheLoopMiddleware` was declined
for plan approval and **"not declined for per-call approval of an irreversible action, which is a
different, still-open question."**

`ConnectorJobWorkflow` suspends on a child `AwaitAnswerWorkflow` before it acts — the **second
caller** of the durable wait, which is what that primitive was argued for. Per call rather than per
plan, because that is what irreversibility means: a plan approved an hour ago authorised a *kind* of
work, and filing this deviation with these arguments is a particular act. A refusal or an expiry
fails the job and attempts nothing; the deadline is three days rather than the wait's ninety-day
ceiling, because an approval nobody answered in three days is a decision that was not taken and the
arguments approved a week ago describe a situation that has moved.

### Declaring an effect cannot leave it un-gated

`expensive` is what puts a job in `authorize_trigger`'s set, so a manifest could otherwise declare an
external effect any authenticated user could trigger. The manifest is **refused** rather than
silently corrected: `expensive: false` beside an `effect:` block is an author who believed one of the
two, and which one they believed matters.

### The ledger records the attempt *before* it is made

`effects` is written on the way in and updated on the way out. **A row left in `attempting` after a
crash is the honest state**, not a defect: this system may have filed the deviation and lost the
acknowledgement. A ledger that recorded only successes would answer "nothing happened" for exactly
the case an operator most needs to investigate, which is why `unsettled` has an index of its own and
carries the sentence saying what those rows mean.

Two details the tests pin: an `applied` row is never walked back to `attempting` by a replay, and
`external_ref` is stored even on a **failure** — it is the far side's own handle and the only thing
an operator can undo by hand, so losing it because the call failed after the record was created is
the worst possible time to lose it.

## Consequences

- **Nothing in this repository declares an effect, and a test asserts that.** Every job here writes
  this system's own stores; declaring an effect on one would be a false claim about what it reaches,
  and a shipped example would put an external write on the surface of every deployment. The
  declaration is for a site that has a system to reach.
- The two carried fields (`effect_system`, `effect_reversal`) are additive on `ConnectorJobInput`
  and default to empty, because they cross the Temporal wire and histories are in flight — the rule
  `plan_step` and `timeout_seconds` already follow.
- The ledger never fails the job: a settle that cannot write logs and leaves the row unsettled,
  which says the far side's state is in doubt. After a ledger outage it genuinely is, and raising
  would fail a job whose real work already succeeded.
- What is still **not** built, and is a decision rather than an omission: nothing *runs* a
  compensation. Declaring one names the job that undoes this one; invoking it is a person's call
  through the ordinary launcher, because an automatic rollback is a second irreversible act taken
  without the approval the first one needed.
