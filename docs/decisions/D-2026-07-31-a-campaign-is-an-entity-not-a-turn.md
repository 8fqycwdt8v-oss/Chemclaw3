# D-2026-07-31-a-campaign-is-an-entity-not-a-turn — A campaign is an entity, not a turn

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-024 (the agent designs experiments),
D-141 (the correlation header), D-158 (`calc_refs` on a computed note)

## Context

`suggest_next_experiment` is, by its own docstring, "the one-shot human-in-the-loop suggestion" —
the path the conversational agent actually uses. It takes a decision space and a run history,
featurizes the categoricals, fits a surrogate, returns candidates, and **writes nothing**.

The GP fit is milliseconds. The expensive part of an optimization is a chemist and an agent jointly
framing the problem out of scattered history: which variables matter, what their ranges are, which
past runs are comparable enough to seed it. That framing was discarded at the end of every turn, so
the next question rebuilt it from scratch — and the sequence of proposals, which is the only thing
that could ever show whether the suggestions were any good, existed nowhere.

Two things made it worse than a missing feature.

`knowledge/optimization-campaign/` notes already existed, from **retrospective DRFP clustering** of
ingested reactions (`memory/optimization.py`) — a mechanism with no identity link to any BO run. So
the system had a word for a campaign, a note type named after it, and no object behind either.

And the path that *did* persist something was the wrong one. `BoCampaignWorkflow` produces a
PR-gated note, and it runs two demo objectives (`reizman_suzuki`, `solubility_max`) — neither an
optimization a chemist runs. The path with real users persisted nothing; the path that persisted
was a simulation.

## Decision

**A campaign is a durable entity identified by its problem, and every suggestion is appended to
it.** `bo_campaigns` + `bo_suggestions` (`infra/sql/031`), written by `suggest_next_experiment`
itself.

**`campaign_id` is a hash of the decision space and the objective.** This is the choice everything
else follows from. Minting an id per call would have produced a table of unrelated rows — a log,
not an entity. Deriving it from the problem means three refinements of one optimization accumulate
against one campaign, *without anyone having to start one first*, which matters because a chemist
does not know at the first question that they are beginning a campaign. Two people optimizing the
same space converge on the same row, which is correct: it is the same campaign.

**Descriptors are excluded from the identity.** They are computed *from* the structures, so a cache
miss that recomputes them to the same values must not fork the campaign, and a calculator upgrade
shifting one in the sixth decimal is not a new optimization problem. The structures themselves are
included: swapping a ligand for a different molecule is a different space.

**Suggestions append; they never overwrite.** A second ask with more observations is a new proposal,
and each row keeps the observations it rested on — the same candidate proposed from three runs and
from thirty means different things, and the comparison is the point.

**The tool returns a richer object.** `ExperimentSuggestion` carries the candidates, the
`campaign_id` and the `calc_refs`. The candidates alone were never the problem; what was missing
was any handle carried out of the call. The skill now tells the agent to quote the id back, which
is how a later turn adds the run that was actually done.

**`calc_refs` reaches the BO path.** `run_cached_properties` derived a `CalculationKey` and threw it
away, so the featurization could not say which calculations its descriptors came from. It now
returns a `CachedProperties` named tuple — named, so the existing two-value unpacks fail loudly
rather than silently binding `cached` to a key string — and `featurize_problem` hands the keys up.
This is D-158's move, on the other computed path.

**The connector can read its caller.** `chemclaw.connectors.caller` binds the advisory
`X-Chemclaw-*` headers into contextvars in `CallerLogMiddleware`, so a tool body can stamp a record
with the conversation that asked for it. That is precisely what D-141 sent those headers for — the
middleware's own docstring said "so a connector's own records can be joined to the core audit
trail" — and until now a connector could only put them in a log line. The trust rule is unchanged
and restated at every reader: authorization happened in core, these are attribution and never a
gate.

## Consequences

An optimization is now a thing with a history: what was proposed, on what evidence, by whom, in
which conversation, and which calculations shaped the space. `chemclaw explain <session-id>`
reaches it through `session_id`, and an `experiment-proposal` note drafted from a suggestion can
cite its `calc_refs`.

**Recording never costs the suggestion.** `record_suggestion` swallows its own failures — the
chemist asked for candidates, and a database blip must not turn that into an error. It still returns
the campaign id, because the id is a pure function of the problem and remains the right handle even
on the turn where the write did not land. The same trade `agent/audit.py` and `kg/proposal.py` make.

**What this does not do, stated rather than implied.** There is still no path from a proposed
candidate to *what was actually run and measured*. Closing that needs a rule for deciding when an
ingested `reaction` note is the execution of a given candidate — a match over conditions with
tolerances, on parameters an ELN records inconsistently — and getting it wrong attributes a result
to an experiment nobody ran, which is worse than the open loop. It is the remaining half of the
backlog row and it wants its own decision. Likewise, whether the two demo objectives should ship at
all is a question this leaves open: the inline path is now the one that persists, which was the
inversion worth fixing first.
