# `skills/` — Agent Skills (domain judgment)

**Responsibility:** "how do I do X" — domain judgment, loaded on demand via the
`SKILL.md` standard (progressive disclosure keeps agent context lean). A Skill
decides *when and how* to use capabilities; it never re-implements them and never
touches storage directly (it goes through an MCP tool — gate G6).

Each skill is a subdirectory with a `SKILL.md` (front-matter + instructions).
This directory holds **Markdown, not Python**. See `docs/architektur.md` §3,
§12.3.

Empty until Phase 1 loads the first skill (plan step 1.5); more in Phases 2, 3, 4, 5.
