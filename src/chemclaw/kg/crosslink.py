"""What calculations a note rests on, counting the ones it cites only through an artifact (STO-7).

`Note.calc_refs` points a note at the calculations behind it, and `artifact_refs` points it at a
by-product of one — so "which calculations does this note rest on" is not a field read, it is a
field read plus the run each cited artifact came out of. `cited_calculations` is that one
definition, and it exists because two readers would otherwise each decide for themselves whether an
artifact counts.

**The reverse index that used to live here is deleted, and this paragraph is why.** `calc_ref_index`
and `notes_for_calculation` answered the other direction — given a calculation key, which notes rest
on it? — and had **no caller in `src/` from the day they were written** (D-133) until the day they
went. D-158 gave them one in the `qm` bundle's note builder, and
`D-2026-08-26-semiempirical-is-the-whole-tier` deleted that bundle whole, which took the producer
with it and left nothing on either side. Two ADRs had deliberately *kept* them —
`D-2026-08-05-three-searches-that-disagreed-about-one-note` on the grounds that each was the only
read path for a capability a merged ADR claims, and
`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` on the line "a thing no configuration can reach
is dead, a thing a deployment selects is not". The first premise no longer holds: since
`agent/graph_tools.NoteRef` carries `calc_refs` and `artifact_refs`, STO-7's claim — that the
calculation store and the knowledge graph can reference each other — has a live read path in the
direction the data actually flows, reaching the model, `GET /notes/{id}` and the chemist. The second
was never true of these two: no setting selects them.

**Re-adding the reverse lookup is a new decision, and the ADRs that designed it stand.** What was
measured when it was tried as an agent tool, so the next attempt starts from it: the tool schema
costs **256 tokens of static prefix on every model call** against a ratchet
(`tests/test_context_floor.py`) that had 423 tokens of headroom to give when that was measured
— read the live figure off the ratchet rather than this sentence, which is three waves old and
whose subject read 610 at the last measurement. It cannot borrow `NoteSearch` without
lying — that type's `verdict` tells a caller with no hits that "a differently-worded term may still
find it", which is true of a substring query and nonsense about a cache key. A reverse lookup wants
its own answer shape, and it wants a question somebody is actually asking.
"""

from chemclaw.kg.note import Note


def cited_calculations(note: Note) -> list[str]:
    """Every calculation key this note rests on, deduplicated in first-seen order.

    An artifact reference contributes the key of the calculation that produced it: `artifact_refs`
    is `<calc_key>#<name>`, and the part before the `#` is a citation of that run whether or not
    the note also listed it outright.
    """
    ordered: dict[str, None] = dict.fromkeys(note.calc_refs)
    for ref in note.artifact_refs:
        key, _, _ = ref.rpartition("#")
        ordered.setdefault(key, None)
    return list(ordered)
