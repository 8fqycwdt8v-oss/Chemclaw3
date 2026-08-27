"""`make proposals-reconcile`: the review surfaces disagree-detector (D-2026-08-27).

The proposal record and the git corpus are two views of one gate, and two paths let them diverge
without an error anywhere: `POST /proposals/{id}/decision` with `approved=true` records `merged`
while the actual merge happens — or does not — in the git host, and a webhook delivery for a merge
that was later reverted leaves the row `merged` with the note gone. A row that says a human merged
content the corpus does not hold is precisely the claim the compliance table exists to make
truthfully, so the divergence deserves a check a person can run rather than an argument.

Read-only, deliberately: this reports, it never repairs. A `merged` row whose note is absent has
at least two true stories (the approval was recorded and the merge forgotten; the merge happened
and was reverted) and they call for different actions — only a person holding the git host's
history can tell which.
"""

import asyncio
import sys
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.kg.graph import load_notes
from chemclaw.kg.proposal import NoteProposal, ProposalState, proposal_store

_PAGE = 200


async def merged_but_absent(notes_dir: Path) -> list[NoteProposal]:
    """Every `merged` proposal whose note id no current corpus file defines."""
    on_disk = {note.id for note in load_notes(notes_dir)}
    missing: list[NoteProposal] = []
    store = proposal_store()
    before_id: int | None = None
    while True:
        page = await store.listing(ProposalState.MERGED, "", _PAGE, before_id)
        if not page:
            return missing
        missing.extend(row for row in page if row.note_id not in on_disk)
        before_id = page[-1].id


def main() -> int:
    """Report merged rows the corpus does not hold; non-zero when any exist."""
    notes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.knowledge_path
    if not notes_dir.exists():
        print(f"notes directory does not exist: {notes_dir}")
        return 1
    rows = asyncio.run(merged_but_absent(notes_dir))
    for row in rows:
        decided = (
            f" (decided {row.decided_at:%Y-%m-%d} by {row.decided_by})" if row.decided_at else ""
        )
        print(
            f"proposal #{row.id}: note {row.note_id!r} is recorded merged{decided}, "
            f"but no such note is in {notes_dir} — either the merge never happened in the git "
            "host, or it was reverted; the record should say which"
        )
    if rows:
        print(f"\n{len(rows)} merged row(s) the corpus does not hold")
        return 1
    print(f"OK: every merged proposal's note is present in {notes_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
