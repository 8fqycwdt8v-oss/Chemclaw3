# D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate — Routing is scored on the surface a turn used, not on whether it delegated

Why the team delegates once or twice in fifteen whatever the prompt says, why routing *quality*
could not be measured the way it was being asked for, and what replaces that measurement.

**`agent_teams_enabled` stays `False`** — the same conclusion as before, on a different and better
reason, with the cost objection that used to carry it withdrawn.

## The question this was supposed to answer

D-2026-08-10 deferred the team's default to a measurement: "a supervisor that mis-routes is worse
than the agent it replaces, and no unit test can establish which of those a deployment gets". The
first live run answered half of it — the team arm delegated **1 of 15** probes at 31% more tokens —
and left the other half open, because accuracy on a denominator of one is not a number. The
backlog row asked for "a probe set that provokes delegation rather than hoping for it".

## Two real defects, found and fixed, that changed nothing

Both were found by looking at what the supervisor is actually sent, and both are worth keeping.
Neither moved the delegation rate, which is why they are reported here as a null result rather than
as the fix.

**The routing menu carried no capability information.** `_description` took
`instructions.split(". ")[0]`, and all five shipped profiles open with the same identity sentence —
"You are Chemclaw's `<name>` specialist". Upstream renders the menu as `- {name}: {description}`,
so the supervisor was choosing between five descriptions that told it nothing the name had not
already. The capability sentence is the *second* one in all five. Fixed by dropping a leading
sentence that carries no information beyond the name. **Measured after: still 1 of 15.**

**The `task` tool described a different mechanism.** Upstream's description opens "Launch an
ephemeral subagent to handle complex, multi-step independent tasks with isolated context windows" —
delegation as a context-window optimisation, for when a job is big. Chemclaw's specialists are a
capability partition with a mandatory safety gate, where the question is not "is this big enough to
isolate" but "whose surface answers this". Chemclaw already overrode the *system-prompt* half of
upstream's guidance and had never overridden the tool description, so the supervisor read one
policy in its system prompt and a different one on the tool it had to call. Fixed with a
Chemclaw-authored `task_description`. **Measured after: 2 of 15.**

## What it actually is

**The supervisor holds every tool every specialist holds.** `reject_widening` requires
specialists ⊆ supervisor, and enforces it at build time — that is invariant 1 of
D-2026-08-10, and it is a security property nobody should want removed. Its consequence for
routing had not been drawn: the supervisor is never *missing* the tool that answers a question, so
delegating is always a strictly longer path to a tool already in hand. No prompt makes that a good
trade on a one-tool question, and the model declining it is the model being right.

The measurement says exactly this. Across three arms, **every delegation that happened was to
`safety`** — 2 of the 3 safety probes, 0 of the other 12. `safety` is the one specialist the
supervisor prompt gives an unconditional reason to consult: "Ask `safety` whenever the work
involves handling a substance, whether or not the chemist raised it." It is the only instruction
that does not depend on capability, and it is the only one that produced delegations.

So **spontaneous delegation is not a measurable quantity on this architecture**, and a probe set
built to provoke it would be measuring the strength of its own phrasing. That is the same defect
the DARK-1 probe was fixed for one file away, arrived at from the other direction: there, a check
depended on the model volunteering a re-plan; here, on the model volunteering a delegation.

## The decision

**Routing quality is scored on the surface a turn used, not on whether it delegated.**

`RoutingScore` gains `self_answered` / `within_expected_surface` / `outside_expected_surface`: for
every turn the supervisor answered itself, whether the tools it reached for are ones the expected
specialist advertises. This needs no delegation at all, and it answers the half of "routing
quality" that is a property of the partition — *was `expects_specialist` the right answer* — while
leaving the delegated turns to speak for the supervisor's judgement, which is all they ever could.

Measured on the same run that delegated twice:

| arm | delegated | correct | self-answered | within expected surface |
|---|---:|---:|---:|---:|
| single | 0 / 15 | — | 12 | 10 (83%) |
| team | 2 / 15 | 2 | 10 | 8 (80%) |

**And it immediately found something the accuracy number structurally could not.** The same two
probes fall outside their declared surface in *both* arms — `rt-13` reaches `find_past_jobs` and
`gather_evidence`, `rt-14` reaches `gather_evidence`, both expecting `reporting`. Appearing in both
arms is what identifies it as a corpus property rather than a supervisor one: writing a report
about work requires finding the work first, so those questions span `evidence` and `reporting` and
the corpus's single-name `expects_specialist` cannot express that. A supervisor that had delegated
`rt-13` to `reporting` would have handed it a specialist without the tools to start, and been
scored *correct* for it.

## The cost objection is withdrawn

The earlier "team costs 31% more" compared two arms that were both measured before prompt caching
existed. Re-measured with both arms on one build:

| arm | tokens | vs single |
|---|---:|---|
| single (control) | 1,779,642 | — |
| team | 1,745,087 | **−1.9%** |

The team arm is now marginally *cheaper*, and 15% of that came from the `task_description` rewrite
alone: upstream's description is 6,573 characters, Chemclaw's is ~1,100, and the difference is paid
on every model call of every turn. The three arms cost **$0.96 in total**, against roughly $2.30
for a single arm before caching — the same 15 questions, 84% cheaper, which is what
D-2026-08-12-the-prefix-is-static bought, measured on a real workload rather than a replay.

**So the flag stays off for a different reason than it went off.** Not "the team costs more" — it
does not. It is that at 2 delegations in 15 the team is not doing anything, while carrying five
compiled specialists and a widening guard on every turn. A capability that is nearly free and
nearly never used is still not worth enabling by default; what it is worth is being honest about
why.

## What would actually change it

Not a prompt. The lever is the premise: a supervisor narrowed to *not* hold what its specialists
hold would have to delegate, and delegation would become a capability question rather than a
stylistic one. That inverts `reject_widening` — the supervisor would become the union of its
specialists rather than a superset of each — and it is a redesign of D-2026-08-10's invariant 1,
not an adjustment to it. It is recorded in `docs/planning/DEFERRED.md` with that trigger, because
the thing that would justify paying for it is a deployment where the specialists' surfaces are
genuinely disjoint, and this one's are not.
