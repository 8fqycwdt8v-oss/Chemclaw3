"""What calculations a note rests on, counting the ones it cites only through an artifact.

`calc_refs` names calculations directly; `artifact_refs` names a by-product of one, so the full set
is a field read plus the run each cited artifact came from. `cited_calculations` is the one
definition, so readers do not each decide whether an artifact counts. There is deliberately no
reverse index (calculation key to notes): nothing asks that question.
"""

from chemclaw.kg.note import Note


def cited_calculations(note: Note) -> list[str]:
    """Every calculation key this note rests on, deduplicated in first-seen order.

    An `artifact_refs` entry is `<calc_key>#<name>`; the part before `#` counts as a citation.
    """
    ordered: dict[str, None] = dict.fromkeys(note.calc_refs)
    for ref in note.artifact_refs:
        key, _, _ = ref.rpartition("#")
        ordered.setdefault(key, None)
    return list(ordered)
