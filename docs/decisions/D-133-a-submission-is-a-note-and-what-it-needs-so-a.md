# D-133 — A submission is a note and what it needs, so a computed result can cite the compound it is about

**Context.** `connectors/qm/knowledge.py` documented its own limitation precisely: it emitted no
wikilink to the compound a calculation was about, because a dangling link fails `kg-validate` on
the very PR that adds the note. The compound note might not exist yet, and there was no way to
create it in the same change.

That single constraint made the calculation store and the knowledge graph disjoint. "What we
computed" and "what we know" could not reference each other in either direction — a stale
calculation could not be traced to the conclusions drawn from it, and a conclusion could not be
traced to the run behind it. In a GxP system that is a provenance gap, not an ergonomic one.
`memory/supersede.py` hit the same wall and worked around it by naming a replacement in plain text.

The cause was one field. `NoteSubmission` was exactly one `path` plus one `content`.

**Decision.** A submission carries `files: list[NoteFile]` — the note first, then whatever it
depends on. `propose_note(..., dependencies=[...])` lays them into one PR, so a note and its
targets land in one reviewable unit and one human signs off on both. A dependency already merged
renders byte-identically and produces no diff, so the submission stays idempotent.

The rule is applied **once, at the gate**, not in each connector: `eln.compound.compound_dependencies`
mints the compound note a note links, and `publish_memory_note_activity` (the one path every
machine-written note takes) calls it. A note author states the link; the gate makes it resolve.
Because `compound_id` is derived from the canonical structure, the target is fully determined by
the SMILES the note already carries.

**`calc_refs` and `artifact_refs` are frontmatter, deliberately not wikilinks.** They point *out*
of the graph into Postgres. Making them edges would reintroduce, from the other side, the exact
dangling-link failure this decision removes. They are shape-validated at the schema — prose like
`"the GFN2 run"` in a provenance field is a crosslink nothing can resolve, and it should fail at
the gate rather than pass review looking informative. Whether the target *exists* is a question
only a database can answer, and `kg-validate` runs in CI without one; making it need a database
would be a worse regression than the gap it closes.

`kg/crosslink.py` is the reverse direction — calculation key to the notes resting on it — and is
nine lines over the already-cached parsed notes rather than an index, because a second store here
would be a derived index of a derived index. An `artifact_refs` entry contributes the key of the
run that produced it, so a note citing only a Hessian is still found by a question about its
calculation.

**Rejected: convenience `path`/`content` properties on `NoteSubmission`.** They were written and
removed. A read-only property shadows anything `model_copy(update=...)` writes, so the old field
names kept resolving and silently ignored the update — a real test caught it. One shape, no
aliases.

**Left alone: `memory/supersede.py`.** Its plain-text replacement marker could now be a
`superseded-by` edge, but its choice is deliberate and documented: the replacement is itself an
unmerged proposal in the same run, published as a separate note by the fan-out, so a link would
dangle if a reviewer merged the supersede PR first. Churning a working GxP path for a marginal gain
is not warranted; D-134 makes the alternative available when the fan-out is revisited.

**Verification.** `tests/test_crosslink.py` writes both files of a real submission to disk and runs
the actual validator over them — no dangling link — with the negative control that the note alone
would have failed. The assertion in `tests/test_knowledge.py` that used to read
`note.outgoing_links() == []`, with a comment explaining why the link was impossible, now asserts
the link and its dependency.

---
