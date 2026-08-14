# D-2026-08-13-an-unverifiable-actor-is-recorded-as-a-claim — A writer that cannot authenticate its caller records `unverified:<id>`, and erasure knows both spellings of one person

**Status:** accepted · **Date:** 2026-08-13 · Narrows the `X-Chemclaw-Actor` finding in `D-2026-08-06-a-gate-that-names-nothing`; amends `D-2026-08-08-the-conversation-is-erasable-the-record-is-not`.

## Context

`bo_campaigns.opened_by` and `bo_suggestions.actor` are the GxP answer to "who framed this
campaign's decision space and who proposed this experiment". They are written by two different
paths, and only one of them has ever seen a validated principal.

- **The durable path.** `connectors/bo/workflows.py:163-178` reads `requested_by` off the run's
  Temporal **memo**, which core sets from the front-door principal. That value crossed no
  attacker-writable surface.
- **The synchronous MCP path.** `suggest_next_experiment` calls `caller_provenance()`, which reads
  `X-Chemclaw-Actor` off the serving HTTP request. `chemclaw.connectors.caller` says in its own
  module docstring that these values "arrive on an unauthenticated header from outside this
  process's trust boundary", and the `bo` manifest declares `auth: mode: none` — so the pod does not
  authenticate *core* either. Anything that can open a socket to it can name any chemist it likes.

Measured before the fix: a call carrying `X-Chemclaw-Actor: victim-oid` wrote `victim-oid`
**verbatim** into both columns, byte-identical to a row the durable path had written from a
validated identity. Two writers, two very different warrants, one indistinguishable record.

## Decision

**1. The synchronous path records `unverified:<claim>`.** `_recorded_provenance()` stamps a single
literal prefix onto the actor before it reaches `record_suggestion`. The two writers of that column
now say which of them could vouch for the name it holds.

**2. Marking, not blanking.** Dropping the claimed name would destroy the only join a forensic
reader has — it is still the id that was asserted, and it still correlates with the session and
correlation ids beside it. Marking is also the move this codebase already makes everywhere else:
*the system flags, it never certifies* (the safety layer's invariant, D-080). A record that says
"someone claimed to be alice" is strictly more useful than one that says "someone", and strictly
less dangerous than one that says "alice".

**3. Not a setting.** The marker is part of the *shape of a written record*. A prefix that varied
per deployment would make the column unreadable across two of them, and would make the erasure
sweep below unwritable.

**4. `session_id` and `correlation_id` pass through unmarked.** They are join keys, not attribution
— and they are what lets an auditor recover the *validated* actor from core's own audit trail,
which is the same recovery `record_campaign_run` argues for when it declines to duplicate a session
id.

**5. An absent actor stays absent.** Empty means "not recorded" (a test, a CLI, a direct call);
stamping the marker onto nothing would manufacture a claim where none was made.

**6. Erasure matches both spellings of one person — by exact equality against a closed set.**
`agent/leaver.py::_actor_forms` returns `[base, "unverified:" + base]` and every statement moved from
`= %(actor)s` to `= ANY(%(actors)s)`, which is the *same* comparison applied to each element. Either
spelling may be named by the operator, because an operator pastes what they read out of the column
or out of a previous report.

## Why marking beats the alternatives

- **Refuse the call when no principal is available.** It would take `suggest_next_experiment`
  offline for every deployment, since *no* deployment authenticates its connectors today. A control
  that turns off the feature is not available to be adopted.
- **Source a real principal here.** There is nothing to source. A synchronous MCP call carries
  headers and nothing else: no memo, no token bound to the user, no signed assertion. Inventing one
  is connector bearer/OIDC authentication — a separate, still-open piece of work (BACKLOG: *every
  shipped connector is unauthenticated*), and this decision does not pre-empt its design.
- **Write the service identity instead.** It is true and useless: it erases the only signal about
  who asked, and it would make every inline suggestion look like the system's own idea.

## The regression this would have introduced, and what closes it

Marking splits one person's id into two strings **in a database whose erasure sweep matched actor
columns byte-exactly**. Left alone, an offboarding report for `oid-carla` would have counted the
durable rows and silently missed the inline ones — an under-count of rows that still contain that
person's identifier, which is the one number
`D-2026-08-08-the-conversation-is-erasable-the-record-is-not` exists to get right. A data-protection
answer that is quietly too small is worse than one that refuses.

**The dangerous fix was written, run, and rejected on the measurement.** `LIKE '%' || actor || '%'`
covers both spellings in one line — and also matches `oid-erik-2` when erasing `oid-erik`. With that
version in place, `test_erasing_one_person_spares_another_whose_id_contains_theirs` reports **3**
retained campaigns where **1** is the truth, and takes the bystander's sessions, preferences and
subscriptions with it. `LIKE actor || '%'` fails identically; a `column LIKE 'unverified:%'` guard
plus string surgery in SQL reimplements `_actor_forms` where it cannot be tested. Because the
marker's prefix is literal and known, the set of spellings is **enumerable** — and an enumerable set
needs no pattern.

The blank guard moved with it: `_actor_forms(actor)[0]` is checked, not the marked form, because the
marked spelling of a blank id is the *non-empty* string `"unverified:"` and would otherwise sail
through and match every marked row in the database. All three of `"   "`, `"unverified:"` and
`"unverified:  "` are refused before a statement runs.

## Consequences

- **The prefix is duplicated as a literal in two modules and that is deliberate.**
  `connectors/bo/server/tools.py` is a connector bundle and `agent/leaver.py` is core; importing the
  first from the second would make the erasure sweep depend on a bundle a deployment may not enable.
  Two literals is the honest cost of that boundary — and the Rule of Three says a *third* writer is
  when the constant moves to a shared home, not before.
- **Both tiers get the set, not just the two columns that hold a marked id today.** The rule is
  "these strings name one person", which is a property of the id rather than of the table. The next
  `auth: mode: none` connector to write a person-column would otherwise silently escape the sweep —
  the same failure, one table over.
- **Existing rows are not migrated.** Rows written before this change hold a bare, unverifiable id
  and there is no way to tell them apart from a validated one after the fact; claiming otherwise
  would be a fabricated provenance upgrade. What changes is that every row from here on says which
  it is.
- Proven by `tests/test_bo_provenance.py` (a forged header never becomes the bare recorded identity;
  the marker still carries the claimed name; join keys stay unmarked; an absent caller stays absent;
  the durable path still records its validated actor unmarked) and by the two new `tests/test_leaver.py`
  cases above.
