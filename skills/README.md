# `skills/` — Agent Skills (domain judgment)

**Responsibility:** "how do I do X" — domain judgment, loaded on demand via the
`SKILL.md` standard (progressive disclosure keeps agent context lean). A Skill
decides *when and how* to use capabilities; it never re-implements them and never
touches storage directly (it goes through an MCP tool — gate G6).

Each skill is a subdirectory with a `SKILL.md` (front-matter + instructions).
This directory holds **Markdown, not Python**. See `docs/reference/architektur.md` §3,
§12.3.

## Adding a skill (admin)

Drop a `skills/<name>/SKILL.md` through a reviewed commit — discovery is automatic, with no
registration and no allowlist. The agent reads skills through `deepagents.SkillsMiddleware` over a
read-only backend (`src/chemclaw/agent/skill_backend.py`) that also serves each enabled connector
bundle's own `skills/`. Skills can live in more than one directory: set `CHEMCLAW_SKILLS_DIR` to an
OS-path-separator-delimited list (like `PATH`, e.g. `skills:/opt/team-skills`) to add a second,
e.g. team-private, skills directory without code changes; `CHEMCLAW_SKILLS_ENABLED` narrows the
discovered set to named skills.

This directory is one of three tiers. The organisation's tier changes through `POST /skills/org`
(privileged role) and a chemist's own through `POST /skills/mine`; no agent path writes a
`SKILL.md` in any tier — the agent can only `propose_skill`.

### SKILL.md front-matter schema

YAML front-matter between `---` fences. This is **`agent/skill_manifest.py`'s `SkillManifest`**, which
is `extra="forbid"` — any field not in this table fails `make skill-validate`. It is a narrowing of
the [Agent Skills spec](https://agentskills.io/specification): the spec's `license`,
`compatibility`, `allowed_tools` and `metadata` are **not accepted here**, because nothing in this
system reads them and a field the validator ignores is a field that silently rots.

| Field | Required | Notes |
|---|---|---|
| `name` | yes | lowercase letters/numbers/hyphens, ≤64 chars, no leading/trailing/double hyphen. **Must match the directory name** and be unique (a duplicate name is skipped). |
| `description` | yes | ≤1024 chars. This is the L1 text the model sees to decide *whether* to load the skill — make it say when to reach for it. |
| `tools` | no | The capabilities this skill's judgment is written about, **by tool name** — in-process tools, generated connector job launchers, and tools an enabled connector serves, all in one list. Validated in both directions: a declared tool must exist, and a tool the body names must be declared. Omit it only for pure process guidance — see below, because the list is load-bearing. |
| `requires` | no | The subset of `tools` the skill is *centrally* about: the skill is hidden when any of these is absent, not only when all declared tools are. Every entry must also be in `tools`. Empty for almost every skill. |
| `tags` | no | Free-form grouping ("retrieval", "optimization"). Human-facing only — nothing dispatches on a tag. |

### `tools:` decides whether the skill is advertised at all

The declaration is not documentation any more (D-2026-08-05). At build time the agent drops a skill
**every** one of whose declared tools is missing from what that agent advertises — because judgment
about a capability the model cannot reach reads to it as an available path, and it plans around one.

Three consequences for an author:

- **Declaring nothing means "always visible."** That is right for process guidance that depends on
  no capability (`development-report`, `playbook-distillation`) and wrong for anything else, so
  `make skill-validate` fails a skill whose body names a tool the frontmatter omits.
- **Over-declaring is safe, under-declaring is not.** One surviving tool keeps the skill, so a
  generous list can only keep a skill visible; a short list can hide it from an agent that can run
  exactly what it teaches.
- **It grants nothing.** A declaration cannot make a tool callable — `authorize_tool` and the
  profile decide that. It can only cost a skill its place on the list.

### Naming a note type in the body

When a skill tells the agent to write a knowledge note, name the kind as **``type `x` ``** — the
word `type`, then the backticked slug. `make prose-validate` checks exactly that phrasing against
`KNOWN_NOTE_TYPES`, and anything written another way is unchecked. This is not a style
preference: a skill instructing a note type that is not a known type sends a reachable tool to
write an unwritable note (D-164).

### Template

```markdown
---
name: my-skill
description: >-
  One or two sentences on WHAT judgment this skill provides and WHEN to load it
  (the model only sees this text until it opens the skill).
---

# My skill

The step-by-step judgment: how to decide, which tools to call in what order, how
far to trust each, and how to present the result. Reference tools by name; never
re-implement a capability or touch storage directly (gate G6).
```

Validate skills with `make skill-validate` (front-matter against `SkillManifest`, and `tools:`
against the live surface in both directions) and their prose with `make prose-validate`.
