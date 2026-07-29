"""Agent memory layers (plan Phase 5) — episodic and semantic, no new infrastructure.

Built entirely from existing pieces: fingerprint-keyed structural identity (Phase 3), the
canonical reaction schema (Phase 4), and the PR-gate (Phase 2). The **episodic** layer
(`memory.campaign`) chains experiments where one reaction's product is another's reactant and
narrates the chain as a `campaign` note citing its evidence. The **semantic** layer
(`memory.playbook`) distils patterns that recur across >=2 projects into a `playbook` note.
No new note store, no new database — only new note *types*, skills, and background jobs.
"""
