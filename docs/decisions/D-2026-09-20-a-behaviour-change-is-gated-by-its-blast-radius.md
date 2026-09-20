# D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius — the organisation's skills tier, and who approves one

**Status:** accepted · **Date:** 2026-09-20 · **Builds on:**
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`,
`D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved`,
`D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool` ·
**Narrows** `D-2026-09-05` §2: promotion into `skills/` in git stops being the only way a skill
reaches everyone.

## The gap

`D-2026-09-05` drew one axis — *does this change what the agent does?* — and gave it one gate with
two destinations. A skill that reaches **one person** is that person's to accept; a skill that
reaches **everyone** goes through an admin, and "everyone" meant `skills/` in git, reachable only by
a reviewed commit and a deploy.

The second destination shipped (`D-2026-09-18-a-skill-a-chemist-keeps…`). The first did not, and its
absence is not a missing convenience: a deployment that learns something worth keeping can put it in
**one chemist's tier or nowhere**. The agent proposes, the chemist accepts, and the judgment stops at
the edge of that person's turns however right it is and however many people re-derive it.

So: an organisation-wide tier, published by the privileged role, read by every turn.
`agent/org_skills.py` and `api/routes/org_skills.py` are it, and `SkillsReadOnlyRefusal` is
untouched — no agent path writes a skill on any tier.

## The decision

**The blast radius decides the approver, and the tier decides the blast radius.**

| Tier | Acts on | Approved by | Written through |
|---|---|---|---|
| `/mine/` | one chemist | that chemist | `POST /skills/mine`, or accepting their own proposal |
| `/org/` | everyone in the deployment | the privileged role (`deps._is_reviewer`) | `POST /skills/org` |
| `skills/` | every deployment | a reviewed commit | git, unchanged |

`_is_reviewer` is the role `D-2026-09-05` §2 already named for this and its own docstring already
predicted the subject: *"The role is also what an admin will hold when a skill is proposed."* No new
role set — the one every gate reads.

## The promotion unit is a document, not a queue entry

This is the part that was decided rather than derived, so the two refused options are recorded.

**Refused: an admin listing of everybody's proposals, plus an `actor` field on the decision.** The
obvious design. It fails on the property `api/routes/proposals.py` states as its reason for existing:
*"Owner-scoped by construction, not by a check. Every handler reads `principal.oid` and passes it to
a store call keyed by it — there is no parameter naming whose queue to touch, so there is no
authorization decision here to get wrong."* An `actor` field converts that structural property into a
checked one, on the highest-consequence route in the module. It fails a second time on where the
write lands: `_write_what_was_accepted` writes the accepted body into a tier belonging to the
*decider*, so an admin deciding alice's proposal either writes into alice's namespace — an unreviewed
instruction reaching a person's turns without that person's act, which is exactly what §3's exemption
is conditioned against — or into their own, which is not what either party asked for. Making it
correct means one route with two destinations chosen by role, which is the "second door into one
bound" shape `validated_skill` exists because of. It also puts a third party's oid into
`record_refusal`'s `target`.

**Refused: a `kind="org_skill"` the agent proposes directly.** `behaviour_proposals` is keyed
`(actor, kind, name, content_hash)` and an org proposal has no owning actor. Setting `actor` to a
sentinel breaks "content identity is per actor" and breaks `leaver`'s `(actor, decided_by)` sweep,
so a departing chemist's org proposals become unfindable — a real regression in what an erasure
report can honestly say. Keeping `actor` as the proposer leaves two chemists proposing identical text
as two rows an admin must decide twice, with one left open, which is the two-open-versions state
`_SUPERSEDE` and the advisory lock exist to prevent one scope down. And it would let the *model*
address the deployment-wide queue on evidence it cannot have: it sees one chemist's sessions, which
is `D-161`'s self-confirming loop wearing the costume of cross-project evidence.

**Taken: the owner accepts to their own tier; an admin promotes the body.** The chemist reads it back
with `GET /skills/mine/{name}` and an administrator posts it to `POST /skills/org`. Every route in
the system stays owner-scoped by construction, no admin writes into a person's namespace,
`behaviour_proposals` needs no column and no migration, and an org skill has **two** humans behind it
— the one who ran it on their own turns first, and the one who decided everybody should. That last
is `D-2026-09-05` §3's own sequence ("live for its own user before review, and global only after"),
re-sited onto the tier it was written for.

**The cost is real and is recorded rather than hidden**: there is no in-product way for a chemist to
*request* a promotion. They send an admin the body. Building a second queue for that is the failure
`D-2026-09-05`'s own Consequences measured, and `docs/planning/BACKLOG.md` carries the row instead.

## What the tier is

Stored, not filed, for `D-2026-09-18-a-skill-a-chemist-keeps…`'s reason exactly: a pod's filesystem is
ephemeral and the chart runs replicas, so a directory would vanish on restart and differ between two
pods answering the same chemist. Two namespaces — `("org-skills",)` for the active bodies and
`("org-skills-versions", name)` for the history that replaces `git revert`
(`D-2026-09-20-a-revert-is-a-pointer-when-there-is-no-commit-to-revert`).

**No actor component**, which does two jobs: it is what makes the tier the organisation's rather than
somebody's, and it is what keeps the system prefix byte-identical between two sessions, which
`tests/test_context_floor.py` asserts of the whole system message.

**It is therefore not swept by `agent/leaver.py`, and that is stated rather than left as an absence.**
An erasure request that finds a departing person's words inside an organisation skill is a content
question for an administrator — a revert, or a retire — not a prefix sweep, because the document is
the organisation's judgment and other people's turns depend on it.
`tests/test_leaver.py::test_an_erasure_does_not_take_the_organisations_judgment` holds it.

Admission is `local_skills.validated_skill` **unchanged**, so all four rules — the char bound, the
frontmatter parse, the name charset, the shipped-name conflict — apply at this door from the one
definition. A second `agent_org_skill_max_chars` was refused: that bound is about what a *skill* is,
judgment rather than a transcript, which is as true of one an admin publishes as of one a chemist
keeps.

Narrowings: `RoleScopedSkills` applies (free — a contextvar read per reach, and a name absent from
`skill_role_gates` is ungated), and the `skill_names == frozenset()` control arm applies.
`EnabledSkills` deliberately does **not** — it names *shipped* skills, so a deployment setting
`CHEMCLAW_SKILLS_ENABLED` would delete this tier outright, which is the trap `agent/local_skills.py`
already names one tier over. `ToolScopedSkills` is **deliberately not built** in this change: it
needs each body's frontmatter parsed out of the store inside a possibly-synchronous `ls`, and the
personal tier has carried the same gap with a backlog row since it shipped.

**Revisit when:** `docs/planning/BACKLOG.md`'s parse-on-write row lands for either stored tier — the
cheap shape is to keep the declared tools beside the body at write time, and it fixes both tiers at
once.

## The collision rule, in both directions

Three tiers mount side by side and upstream resolves a name collision **last-source-wins**, so
`_skills_middleware` orders its sources by ascending review depth: `/mine`, `/org`, then the git
labels. One person, then an administrator, then a reviewed commit.

That makes the two directions asymmetric, and the asymmetry is decided rather than incidental. A
personal skill **may not** take a name the organisation publishes — it would never act, so writing
one is writing judgment that silently does nothing, and `save_local_skill` refuses it in the writer
so both doors into that tier have the rule. An organisation skill **may** take a name a chemist
already uses privately: an administrator cannot see one person's private vocabulary, and a
deployment-wide publication blocked by it would be blocked by something nobody can find. The person's
own document is untouched and still visible on the route that lists it.

## The gate the stored tiers did not have

Building this found the personal tier narrowed **in the prompt only**: `_skills_middleware` decided
whether to advertise `/mine`, and the backend under it had no predicate at all, so a profile with
`skill_names: []` — the eval control arm whose entire job is removing skills — advertised none and
still served every body to anything that named a path. `download_files` was ungated there too, the
reach path the reviewed tree had already closed while calling it "a latent hole rather than a live
one".

That is a narrowing hole rather than a widening of authority: nothing reaches another person's tier,
because the namespace closes over the actor. What it falsified is `data/evals/profiles/
skills-removed.yaml`, the control arm this repository uses to find out whether skills help at all.

`agent/skill_store.PermittedStoreBackend` is the shared read half both stored tiers now mount, with
`permits` a **required** keyword — a default is precisely how the gap existed. It is a second class
rather than one shared with `NarrowedSkillsBackend` because the base classes disagree about which
verbs are native and which are `to_thread` wrappers, and that disagreement is the whole content of
each one's override list.

## Consequences

- An organisation can keep judgment without a deploy, and `skills/` keeps its meaning: what is true
  of **every** deployment, reviewed in a pull request.
- Every chemist's prefix now carries whatever an administrator published — bounded by
  `agent_org_skills_max`, derived from the fan-out multiplier rather than from the personal tier's
  number, and ratcheted by its own allowance rather than by `CEILINGS`
  (`D-2026-09-20-a-tier-every-prefix-pays-is-still-not-a-ceiling`).
- No migration. The tier is rows in `store` under two new namespace prefixes, so
  `infra/sql/grants/app_privileges.sql` already covers it.
- `chemclaw_skill_loads_total{skill}` carries org rows: the label is safe here where it is not on the
  personal tier, because an org skill's name is deployment configuration rather than one person's
  words, and the row cap bounds its cardinality.
- The tier ships **dark** until `agent_memory_enabled` is on — mounted only where there is a store.
