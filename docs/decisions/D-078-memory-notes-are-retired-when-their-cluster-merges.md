# D-078 — Memory notes are retired when their cluster merges or shrinks

**Context.** `memory.ids.stable_id` anchors a campaign/playbook/optimization note on its cluster's
*smallest* member id (D-070). That is exactly right for **growth** — a grown cluster re-mints the
same id, so periodic re-synthesis updates the note in place through the idempotent PR-gate branch —
and silently wrong for two other transitions. On a **merge**, two clusters become one whose anchor
is one of the two old anchors, leaving the *loser's* note in the graph as a current account of a
subset that no longer exists. On a **shrink** (the anchor member drops out), a new id is minted and
the pre-shrink note stays current beside it. Either way retrieval can serve a stale note as fact,
with nothing linking it to what replaced it — the failure the bi-temporal fields exist to prevent.

**Decision.** `memory/supersede.py::supersede_updates(new_notes, existing, as_of)` — pure — returns
retired copies of merged notes this run replaced: same type as the run's output, an id the run no
longer mints, no `valid_to` yet, and at least one cited member now covered by a new note. Each copy
gets `valid_to = as_of` (`Note.is_current` then drops it from current-evidence sweeps; the note is
never deleted — it stays in Git, reachable by id) and a body line naming its successors.

**Applied in the builders, not at the publish sites.** `memory/jobs.py::_with_supersedes` wraps all
three `build_*_notes` functions, so the in-process job and the durable activity both get it and
neither can forget; the retirement then travels the *same* PR-gate/fan-out path as every other
memory note — no second write path.

**Overlap, not equality, and `valid_to`, not `is_current`.** Overlap catches merges (all members to
one successor) and splits (members to several) alike. Testing `valid_to is None` rather than
`is_current(as_of)` makes the job idempotent — a second run cannot re-close, and re-append its
marker line to, a note it already closed — and still covers a note whose validity begins in the
future (closed at its own `valid_from`, never before it, so the F10-G2 window check holds).

**The successor is plain text, not a `[[wikilink]]`.** The successor is an unmerged proposal from
the same run, so a link would dangle and fail `kg-validate` if a reviewer merged the supersede PR
first — an ordering trap for a human, in exchange for an edge nothing traverses (a non-current note
is already out of retrieval).

**Side effect that closes a manual chore.** BACKLOG recorded a one-time hand-cleanup for notes
minted under the older set-derived ids. Such a note intersects its successor's members under a
different id, so the first run after this ships retires it automatically.
