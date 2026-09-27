# D-2026-09-18-a-cap-below-an-ordinary-turn-is-a-guard-that-kills-another — the corrections to the Paperclip flips, measured

**Status:** accepted · **Date:** 2026-09-18 · Follows and **corrects**
`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has`, which is merged and therefore not
edited. It supersedes none of that decision: every flip stands, the escalation stands, and the
build-vs-adopt conclusion stands. What changes is one number, one count of how many defaults moved,
and two figures about Paperclip — three of the four falsifiable by reading the diff that shipped
them, which is `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` happening to the ADR that
cites it.

## Context

The flips were reviewed with fresh context after merge. The reviews found the code sound; what they
found wrong was arithmetic and counting.

## The cap was set below an ordinary turn

`agent_max_turn_billed_tokens` shipped at `300_000`. That is **3 model calls** at this tree's
`PREFIX_BOUND` of 81,600 tokens per call — while `harness_max_loop_iterations` permits **25**, and
`agent_context_token_budget` permits 118,700 billed tokens on each of them. One guard had made
another unreachable: a turn doing ordinary heavy work would be ended by the backstop long before
the iteration cap it is supposed to sit above.

**Where the wrong number came from is the interesting part.** 300,000 was justified as "above the
one runaway this tree has measured" — the 250,000-token turn against a 1,000-token session cap.
That measurement was taken when a model call carried roughly 10,000 tokens. The prefix has since
grown sevenfold (`D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system`), so
250,000 tokens is now **three calls** rather than twenty-five. A figure copied forward past the
change that invalidated its unit.

The cap is now **derived rather than chosen**: `harness_max_loop_iterations ×
agent_context_token_budget` = 2,967,500, rounded to `3_000_000`. That is the most a turn can
lawfully spend with both existing guards holding, so the backstop bounds only a turn that has
escaped one of them — which is what a backstop is. `tests/test_spend_cap.py` pins the *relation*
rather than the figure, in both directions: the cap must be at or above the lawful ceiling, and it
must fund at least `harness_max_loop_iterations` calls at `PREFIX_BOUND`. Both fail against
300,000. A deployment still sets its own number from its `turn_costs` distribution; what changed is
that the shipped one no longer contradicts the two guards it ships beside.

## Four defaults changed, not three

The merged ADR's table has three rows and its own sentence says "Three defaults change".
`answer_review_max_rounds` also moved, from `0` to `2` — it has to, because that ADR's central
finding is that the setting was unreachable behind two off switches, and turning the switches on
without setting the rounds leaves the loop at zero passes. The flip is in the diff; only the count
was wrong. Four settings moved.

## The escalation's routing was wrong, and its own argument did not cover it

The merged ADR leaves `asked_of` empty — *"whoever is entitled"* — arguing that a review is not an
authorization, so the requester answering their own is fine. That argument is about
**authorization** and is still sound. It never touched **visibility**. Unrouted does not mean
"whoever is entitled" to a reader: `_may_answer` returns `True` for any authenticated caller and
`pending_store`'s list predicate carries `OR asked_of = ''`, so the request was listed to the whole
tenant — carrying model-authored claim text lifted out of a conversation those readers cannot open,
since a session 404s a non-owner with no existence leak. It is routed to the requester now. The old
argument survives as the reason the requester is a *legitimate* answerer rather than as the reason
nobody is named.

## Two figures about Paperclip, re-read rather than remembered

Both were checked against `docs.paperclip.ing` on 2026-09-18.

**"Ten of its twelve adapters shell out to a vendor CLI" — it is nine.** The built-in table lists
twelve: `claude_local`, `codex_local`, `gemini_local`, `cursor`, `opencode_local`, `pi_local`,
`hermes_local`, `grok_local`, `kimi_local`, then `openclaw_gateway` (a WebSocket gateway), `process`
(a generic shell invocation) and `http` (a webhook). Nine shell out. The argument the figure serves
— that Paperclip is a control plane which does not itself run agents or call models — is unaffected
by one, which is exactly why the digit was easy to get wrong and worth correcting anyway.

**"`dangerouslySkipPermissions` defaults to true for one of them" is literally true and
understates the posture.** `claude_local` documents it: *"Defaults to `true` because Paperclip runs
Claude in headless `--print` mode."* But `grok_local` ships `alwaysApprove` defaulting to `true`,
described by its own page as that adapter's unattended-execution policy — the same posture under a
different field name. Named by field, one; named by behaviour, two. Checked and *not* found on
`hermes_local`, `kimi_local` or `openclaw_gateway`. This is a correction of emphasis rather than of
fact, and it is here because the sentence was being used to argue about a security posture, where
"one adapter" reads as an outlier and "two, under two names" reads as a default.

A figure this ADR cannot hold either: these are a third party's documents on their own release
schedule, so re-read them rather than citing this paragraph.

## Consequences

**A derived default survives a change to what it was derived from; a chosen one does not.** The
300,000 was chosen against a measurement whose unit moved underneath it, and nothing connected the
two. The replacement is an expression over the settings it must sit above, asserted as a relation,
so the prefix growing again moves the cap instead of silently re-breaking it.

**Three of these four were falsifiable by reading the commit that shipped them** — the row count,
the routing paragraph, and the cap's own arithmetic. The Paperclip figures needed an external
re-read. That split is the useful one: the first kind is what a fresh-context review of a diff
finds, and the second is what only re-fetching the source finds.
