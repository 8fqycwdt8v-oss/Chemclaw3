# D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has — turning on what the Paperclip review built, and the one thing it could not reach

**Status:** accepted · **Date:** 2026-09-16 · Follows
`D-2026-09-15-a-green-suite-is-evidence-about-the-cases-somebody-constructed`, which fixed those
features, and the four ADRs that introduced them. Supersedes none of them: each decision stands and
this is about whether any of it *runs*.

## Context

Four ideas were taken from an evaluation of **Paperclip** (`paperclip.ing`, MIT, self-hosted), then
deep-reviewed and repaired. A second review then asked the question nobody had: **of the four, how
many are in effect in the shipped configuration?**

Two. The durable per-actor budget (on via the chart) and the premise staleness check
(unconditional). The check-in sweep and the answer revision loop both shipped off.

Worse, one of them could not be turned on by its own switch. `answer_review_max_rounds` is reachable
only behind `verifier_enabled` or `answer_shape_gate_enabled`, and its own comment said so — both
were off, so setting rounds to 2 changed nothing. **A feature with a switch that does not reach it is
not a feature that ships off; it is one nobody can turn on**, and the distinction was invisible
because each setting's comment argued locally and correctly for its own default.

The same review re-asked the adoption question with facts rather than recollection, and that answer
is recorded below because it is the reason none of this is a dependency.

## Decision

### Turn them on, and keep the argument that said not to

Three defaults change. In every case the comment that argued for `off` is **kept and answered**
rather than deleted, because each still bounds the value chosen.

| Setting | Was | Now | The argument that had to be answered |
|---|---|---|---|
| `agent_max_turn_billed_tokens` | `0` | `300_000` | "A wrong number here is worse than no number: the cap ends the turn." Answered by what ending means — `spend_cap.py` jumps `to end` rather than raising, and the runner emits `spend_cap_reached` naming the answer **partial**, so a capped turn hands back what it had. And the alternative was never *no cap* but an *unbounded* one: one turn measured at 250,000 tokens against a 1,000-token session cap, which neither half of `api/budget.py` can see |
| `check_in_enabled` | `False` | `True` | "It writes to a mailbox and a channel a deployment may not have configured." Both halves spent: `GET /check-ins` is the reader whose absence was the original defect, and the sweep now supersedes a requester's unread notice rather than stacking — ~87 unprunable rows per requester over a 90-day wait, before |
| `answer_shape_gate_enabled` | `False` | `True` | "It over-fires, and an unneeded review mark costs trust in every mark after it." Accepted deliberately, because the bargain changed: a mark now leads to a revision and, failing that, to a person, rather than sitting there |

`verifier_enabled` stays **off**. It adds a judge model call to every answer and
`require_verifier_capability()` fails pod startup where the gateway cannot enforce structured
output. The shape gate is deterministic and costs no call, so it is what makes the loop reachable.

**300,000 is a runaway backstop, not a budget**, and the chart says so. It is above the one runaway
this tree has measured and is explicitly not the number to keep: that comes from a deployment's own
`turn_costs.total_tokens` distribution.

### Give an exhausted review somewhere to go

The revision loop bounded agent-to-agent rounds and then shipped the answer still marked, counted
and logged — and told nobody. Paperclip bounds its own review rounds with `maxReviewRounds` and then
**escalates to a named human**, sticky. The bounded half existed here; the escalation did not.

It now opens a durable wait, as the turn's **authenticated actor**, and invents nobody where there
is none — `require_actor`'s reject-if-absent rule, and the argument
`D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late` makes about a Schedule having nobody
to be. Best-effort throughout: a review that cannot be filed degrades and the answer still ships,
because a chemist must not lose an answer to a failed review request.

The dedup subject is the **conversation**, not the answer, so two exhausted turns in one thread join
one wait and a reviewer is pointed at the thread, where the evidence is. That would be wrong for an
approval, which is why `connector_job.py` keys its own on the job id.

`asked_of` is left empty — "whoever is entitled". Paperclip names a sticky human reviewer; this tree
has no reviewer role to name, and `effect_approval_role` answers a different question. Inventing one
would be a control nobody configured.

## Consequences

**A default is a claim about a deployment, and four of this tree's controls were making it
silently.** The pattern is not that the defaults were wrong — each was argued — but that nothing
read them *together*. `answer_review_max_rounds` and `answer_shape_gate_enabled` each justified
being off by pointing at the other.

**It cost a guard its exhaustiveness, for the third time.**
`tests/test_planned_ids_stay_inside_owned_namespace` enables "every conditional job" and its
docstring already records holding vacuously twice. `check_in_enabled` was never patched there, so
`agent-check-in` was absent from the plan and was never compared against the prune namespace at all
— the count read 12 and looked complete. Nothing shipped broken; the test's claim about itself was
false. A count over a hand-maintained enabling list catches a job *added* without being enabled, and
never one that was already conditional when the list was written.

**One layering debt was removed rather than added.** The escalation needed Temporal from `api/`,
where `tests/test_third_party_layering.py::_KNOWN_LEAKS` has four rows whose own prose says the fix
is a launcher in `durable/`. `durable/awaiting.open_wait` is that launcher, with two real callers;
`agent/pending_tools.py`'s leak row is deleted and it no longer imports `temporalio` at all.

## Why none of this is a dependency on Paperclip

The adoption question was re-asked against the product rather than against memory of it, and the
answer is **borrow the designs, do not adopt the system** — for reasons of kind, not preference.

Paperclip is a **control plane that does not run agents and does not call models**; its own glossary
says so. Ten of its twelve adapters shell out to a vendor CLI on the Paperclip host, git worktrees
are its isolation primitive, and `dangerouslySkipPermissions` defaults to true for one of them.
Chemclaw *is* an agent. Adoption means inverting the architecture and running this system as an
HTTP-adapter worker underneath it — and that adapter is the least-finished path it has: shown as
"Coming soon", unsignable ("Paperclip does not sign outgoing webhook bodies today"), and documented
with no response schema at all, so no session, no usage and no cost come back.

Four properties of this deployment it cannot meet, each quoted from its own documentation:

1. **Identity cannot federate.** *"No SSO/MFA yet… There is no single-sign-on, SCIM directory sync,
   or multi-factor step in the current release."* Entra is system-wide here (F4). Its `local_trusted`
   mode is worse than no answer: every unauthenticated request becomes a full-trust board actor.
2. **Authorization is additive-only.** *"There is no 'deny' layer — explicit grants only ever add
   capability"*, and a grant survives a role change.
3. **It holds vendor credentials and injects them as plaintext into agent process environments** —
   the exact inverse of `D-2026-09-04-a-gateway-is-the-only-provider`, under which nothing in `src/`
   dials a vendor.
4. **Its only self-hostable sandbox is alpha and not OpenShift.** `plugin-kubernetes` v0.1.0 on an
   alpha CRD, pods pinned to `runAsUser: 1000` (which OpenShift's default SCC will not grant), and —
   for a no-egress posture — under the default `egressMode` an `allowFqdns` list **is not enforced**:
   the policy *"falls back to allowing all public IPv4 on TCP 80 and 443"*. An allowlist that silently
   means "everything" is worse than none, because it reads as a control.

Its audit trail is an ordinary Postgres table with no tamper-evidence claim, where this one is
append-only by grant; and its export *"deliberately leaves behind"* approvals, cost history and
activity, so portability does not move the compliance record. It is also TypeScript-only: no Python
SDK, no non-JS adapter path.

**This is not the LangSmith case and not the LangGraph case.** LangSmith was declined for being
proprietary with prompt content in a third party. Paperclip is MIT and self-hosted, so that argument
does not apply and an earlier reading of this that leaned on it was wrong. LangGraph and deepagents
are libraries composed *into* this process. Paperclip is a **peer application** — a second runtime,
a second control plane, a second identity store and a second audit trail beside a four-repo family
that already owns all four. That is the reason, and it would survive the product being excellent.

## What is deliberately still not taken

**No org chart.** Paperclip's hierarchy exists to route coding tasks between CLI agents; this tree
deleted its specialist team and challenge panel on a measurement (`D-2026-08-15`) that nothing here
changes. **No BYO-agent adapters, no workspaces, no secret store, no company portability** — each is
either declined by posture (`Bash`, egress) or answers a question this system does not have.
