# D-2026-09-20-a-revert-is-a-pointer-when-there-is-no-commit-to-revert — rollback for a stored skills tier

**Status:** accepted · **Date:** 2026-09-20 · **Builds on:**
`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius` ·
**Supersedes**, for the organisation's tier only, the Consequences sentence of
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` that ties rollback to git. That sentence
stands unchanged for `skills/`, which this does not touch.

## What the git tree was buying

`D-2026-09-05` grants the shared tree its safety in one sentence:

> **`skills/` stays git-resident and human-merged**, so a bad *shared* behaviour change is a revert —
> the rollback property any future skill-evolution loop rests on.

An organisation tier in a Postgres store has no commit to revert, so the property has to be built or
given up. Given up is not an option on a tier that reshapes every answer every chemist gets: the
thing an administrator needs at 3am is not "publish a fix", it is "put back exactly what was working".

Git is buying three separate things, and a replacement has to buy all three:

1. **The previous version still exists.** Not a memory of it, the bytes.
2. **Going back is one act**, not a re-authoring.
3. **Somebody can tell what changed, when, and who did it.**

## What was refused

**`DELETE` alone.** Retiring the skill takes the deployment from bad judgment to **no** judgment,
which is a third state rather than last week's. It buys (2) and neither of the others.

**A direct admin write of the old text.** This is the shape that looks like a rollback and is not:
retyping last week's document is a *re-authoring*, nothing proves the bytes match, and any test
written against it — "the name still resolves afterwards" — passes on a document that is subtly
different. It buys (2) and, badly, nothing else.

**`behaviour_proposals` as the history.** Tempting, because it is already immutable and
content-hashed. Refused twice: under the promotion design there are no org rows in it at all
(`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius` — the promotion unit is a document),
and it is retained through erasure and never pruned, so making it the source of truth for what every
chemist's prompt contains would put an erasure-retained table on the live prompt path.

## The decision

**Two namespaces, and activation is a pointer.**

- `("org-skills",)` holds the **active** body per name. This is the only one mounted, so a turn sees
  exactly the active set and never a retired body.
- `("org-skills-versions", name)` holds **every body ever activated**, keyed by its content hash,
  written once. Never mounted — it is a record a route reads, which is why its documents may be JSON
  rather than `SKILL.md` bodies.

A revert names a hash the system already holds the bytes for. `activate_org_version` refuses a hash
the version namespace does not hold, which is the property that makes this a rollback rather than a
write: **the pointer can only point at history.** It then goes through `save_org_skill`, so a revert
spends the same row cap, takes the same lock and leaves the same version record as any other
publication — a revert is an ordinary activation whose bytes happen to be old.

Retiring and reverting stay different acts. `DELETE /skills/org/{name}` removes the active key and
leaves the history, so it is itself reversible.

Blame is `GET /skills/org/{name}/versions`, and it is open to every authenticated caller rather than
to administrators. `D-2026-09-05` §3 makes inspectability the condition a tier holds its exemption
under; a rollback story the people it acts on have to take on trust is not one.

## Where this is weaker than git, said plainly

`agent_org_skill_versions_max` bounds the history at 20 activations per skill, evicting the least
recently activated. `skills/` can be reverted to any commit; this can be reverted to the last twenty
bodies. That is a real loss and it is taken deliberately, because the alternative is worse in the
direction that matters: the version cap is **evicted** rather than refused, so an administrator is
never told they cannot publish a fix because the skill has been edited too often, which would be a
bound on exactly the wrong thing. The row cap above it is the opposite — refused — because judgment
somebody published must not vanish because somebody else added one more.

Twenty because a skill with twenty distinct bodies behind it has a process problem rather than a
history problem, and because the version anybody actually reverts to is a recent one.

## Why the store rather than a table

No migration, which is a consequence of the design rather than a convenience: the tier is rows in
`store` under two namespace prefixes, so `infra/sql/grants/app_privileges.sql` already covers it and
`make db-migrate` gains nothing to apply. Writes go through `StoreBackend` rather than `store.aput`,
so `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly` keeps holding
and the stored shape keeps one definition.

`updated_at` is the store's own, so "least recently activated" is a fact the store maintains rather
than one this module keeps beside it — the same tiebreak `scratchpad.BoundedStoreBackend` takes for
the same reason.

## What proves it

`tests/test_api_org_skills.py::test_a_bad_publication_is_one_call_away_from_the_bytes_that_stood_before`
asserts **byte-identity** after a revert, driven through the routes an administrator actually reaches.
The byte-identity half is the assertion that matters: a test checking only that the name resolves
passes on the re-authoring this design exists to make unnecessary.
`tests/test_org_skills.py::test_a_revert_cannot_name_a_document_that_was_never_active` holds the
pointer to history, and
`tests/test_org_skills.py::test_the_version_history_is_evicted_rather_than_refused` holds the
asymmetry between the two caps.

**Revisit when:** a deployment asks for a revert target older than
`agent_org_skill_versions_max` activations, or an administrator hits the eviction WARNING
`org_skill.versions_evicted` in anger. Either one says the bound is the wrong shape and the history
wants a table with a retention window rather than a per-skill row cap.
