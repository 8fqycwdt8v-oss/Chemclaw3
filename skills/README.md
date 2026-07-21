# `skills/` — Agent Skills (domain judgment)

**Responsibility:** "how do I do X" — domain judgment, loaded on demand via the
`SKILL.md` standard (progressive disclosure keeps agent context lean). A Skill
decides *when and how* to use capabilities; it never re-implements them and never
touches storage directly (it goes through an MCP tool — gate G6).

Each skill is a subdirectory with a `SKILL.md` (front-matter + instructions).
This directory holds **Markdown, not Python**. See `docs/architektur.md` §3,
§12.3.

## Adding a skill (admin)

Drop a `skills/<name>/SKILL.md` and restart the agent — discovery is automatic
(`FileSkillsSource` scans up to two levels deep; no registration, no allowlist).
Skills can live in more than one directory: set `CHEMCLAW_SKILLS_DIR` to an
OS-path-separator-delimited list (like `PATH`, e.g. `skills:/opt/team-skills`)
to add a second, e.g. team-private, skills directory without code changes.

### SKILL.md front-matter schema

YAML front-matter between `---` fences, per the
[Agent Skills spec](https://agentskills.io/specification):

| Field | Required | Notes |
|---|---|---|
| `name` | yes | lowercase letters/numbers/hyphens, ≤64 chars, no leading/trailing/double hyphen. **Must match the directory name** and be unique (a duplicate name is skipped). |
| `description` | yes | ≤1024 chars. This is the L1 text the model sees to decide *whether* to load the skill — make it say when to reach for it. |
| `license` | no | license name/reference. |
| `compatibility` | no | ≤500 chars. |
| `allowed_tools` | no | space-delimited pre-approved tool names. |
| `metadata` | no | arbitrary key/value pairs. |

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

Validate the graph with `make kg-validate`; skills are validated by the loader at
startup (an invalid front-matter `name`/`description` raises with a clear message).

Empty until Phase 1 loads the first skill (plan step 1.5); more in Phases 2, 3, 4, 5.
