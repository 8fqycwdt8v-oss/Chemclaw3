# `tasks/` — the working memory between sessions

Two files, both required by `CLAUDE.md`'s workflow and both read at the start of a session and
written at the end.

- **`todo.md`** — the current task: its plan as checkable items, what shipped, and a review section
  written when it closes. One task at a time; when a task merges, the next one replaces it and the
  record of the old one lives in its ADR, its backlog entries and git history.
- **`lessons.md`** — the self-improvement log. After any correction, the pattern goes here together
  with a rule that prevents the same mistake. It is reviewed at session start, which is the only
  thing that makes writing it worthwhile.

These are notes, not documentation. The durable record is `docs/decisions/` (why a thing is the way
it is), `docs/planning/BACKLOG.md` (what is still open) and `docs/planning/DEFERRED.md` (what was
consciously postponed, and the trigger that would revisit it). If something in `tasks/` deserves to
outlive the task, it belongs in one of those instead.
