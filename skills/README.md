# `skills/` — Agent Skills (domain judgment)

**Responsibility:** "how do I do X" — domain judgment, loaded on demand via the
`SKILL.md` standard (progressive disclosure keeps agent context lean). A Skill
decides *when and how* to use capabilities; it never re-implements them and never
touches storage directly (it goes through an MCP tool — gate G6).

Each skill is a subdirectory with a `SKILL.md` (front-matter + instructions).
This directory holds **Markdown, not Python**. See `docs/reference/architektur.md` §3,
§12.3.

## Adding a skill (admin)

Drop a `skills/<name>/SKILL.md` and restart the agent — discovery is automatic
(`FileSkillsSource` scans up to two levels deep; no registration, no allowlist).
Skills can live in more than one directory: set `CHEMCLAW_SKILLS_DIR` to an
OS-path-separator-delimited list (like `PATH`, e.g. `skills:/opt/team-skills`)
to add a second, e.g. team-private, skills directory without code changes.

### SKILL.md front-matter schema

YAML front-matter between `---` fences. This is **`agents.skill_manifest.SkillManifest`**, which
is `extra="forbid"` — any field not in this table fails `make skill-validate`. It is a narrowing of
the [Agent Skills spec](https://agentskills.io/specification): the spec's `license`,
`compatibility`, `allowed_tools` and `metadata` are **not accepted here**, because nothing in this
system reads them and a field the validator ignores is a field that silently rots.

| Field | Required | Notes |
|---|---|---|
| `name` | yes | lowercase letters/numbers/hyphens, ≤64 chars, no leading/trailing/double hyphen. **Must match the directory name** and be unique (a duplicate name is skipped). |
| `description` | yes | ≤1024 chars. This is the L1 text the model sees to decide *whether* to load the skill — make it say when to reach for it. |
| `tools` | no | The capabilities this skill's judgment is written about, **by tool name** — in-process tools, generated connector job launchers, and tools an enabled connector serves, all in one list. Validated against the live surface, so a renamed or deleted tool fails CI instead of leaving stale prose. Omit it for pure process guidance. |
| `tags` | no | Free-form grouping ("retrieval", "optimization"). Human-facing only — nothing dispatches on a tag. |

### Naming a note type in the body

When a skill tells the agent to write a knowledge note, name the kind as **``type `x` ``** — the
word `type`, then the backticked slug. `make prose-validate` checks exactly that phrasing against
`KNOWN_NOTE_TYPES`, and anything written another way is unchecked. This is not a style
preference: two skills shipped instructing a `protocol` and an `experiment-batch` note, neither of
which is a known type, so the agent's proposal opened a branch that `kg-validate` then rejected —
a reachable tool writing an unwritable artifact (D-164).

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
