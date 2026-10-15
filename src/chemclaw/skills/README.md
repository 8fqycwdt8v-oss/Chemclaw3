# `skills/` — the code behind Agent Skills, in one place

Not the `SKILL.md` files: those are data, in the top-level `skills/` and each bundle's `skills/`.
This is the code that decides who sees a skill, where each kind of skill lives, who may change it,
and what a turn reads it through. Layer 3 stays judgment in documents; the rule that a skill
changes only by blast radius lives here once.

## The three tiers

| Tier | Where it lives | Changed by | Read by |
| --- | --- | --- | --- |
| `repo` | `SKILL.md` files in the reviewed tree and each enabled bundle | a reviewed commit, never at run time | every turn the narrowing admits |
| `org` | rows in the Postgres store, versioned | `POST /skills/org`, the privileged role | every turn, and every authenticated caller over HTTP |
| `mine` | rows in the Postgres store, per actor | `POST /skills/mine` (or accepting a proposal), its owner | that owner's turns |

No model turn writes any tier: every backend refuses every write verb, and no tool module may
import `store.py` (`tests/test_proposal_tools.py`).

## The modules

| Module | What it holds |
| --- | --- |
| `manifest.py` | What a skill is as a document: `SkillManifest`, the declared and required tools read off the files, `validated_skill` (the one admission rule every door goes through) and `skill_fingerprint`. |
| `access.py` | The one access predicate. Reading: the narrowings (enabled, profile, tool, role, reserved names) and `skill_permits`, one predicate per kind of tier. Writing: `Tier`, `Writer` and `may_write`. |
| `store.py` | `SkillStore`, the three tiers behind one interface, over the per-tier primitives: keys and paging, the writer lock, the organisation's versions, and what the stored tiers declare about tools. Imports no `deepagents`. |
| `backends.py` | What a turn reads through: `NarrowedSkillsBackend` (the reviewed tree), `PermittedStoreBackend` (the stored tiers), the refusals, and `ReloadingSkillsMiddleware`, which re-narrows the listing every turn. |

## What stays outside

The graph builder (`agent/langgraph_agent.py`) walks the trees, computes the narrowing once per
build and mounts the backends; `agent/scratchpad.py` adds the stored tiers to the turn's
composite backend through `SkillStore.mount`. The routes are `api/routes/skills.py` and
`api/routes/org_skills.py`.

## Invariants

- A narrowing is applied on every reach path (`ls`, `read`, `glob`, `grep`, `download_files`), not
  only in the listing, because a model told about one path can ask for another.
- A refused read never says whether the skill exists, and another person's tier is unreachable by
  construction: the namespace closes over the actor, so there is no gate to misconfigure.
- The stored tiers are tenant-scoped by namespace (`core.tenancy.namespace_component`); a
  chemist's namespace is the erasure key `agent/leaver.py` sweeps, and the writer lock goes through
  `db.connection()`.
- `tests/test_skill_tiers_golden.py` pins who sees what and who may change what, as a table.
