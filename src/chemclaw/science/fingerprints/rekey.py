"""Re-fingerprint the rows a definition bump shelved, from what each row already stores.

A `STANDARDIZATION_VERSION` bump is a token in `molecule_definition()` and `reaction_definition()`,
so every row fingerprinted under the old one falls out of similarity search the moment the code
ships (`store.py` refuses to rank across definitions). Until this module the only way back was the
runbook's: delete the corpus's `corpus_cursors` row and re-sync the whole ELN, re-reading every
entry from its source to recover structures the index already holds.

**The row is enough.** A reaction row's `label` is the transformation SMILES as ingested — DRFP
standardizes its species while hashing — so re-fingerprinting the label under the new definition is
the row the next sync would write. A molecule row's `id` and `label` are the structure standardized
under the old version, and standardizing that again under the new one gives the new row: its id
moves exactly when `compound_id` moves, which is what makes the two indexes and the compound notes
agree after a re-key (`memory.compound_rekey` does the notes). Both hold for a bump that discards or
neutralizes more than before; one that needs what the old standard form dropped needs the source.

**Insert-only, like every write the runtime role may make on these tables.** The shelved rows stay
where they are — `durable/retention.py` records why nothing reclaims them — and a re-keyed row is an
ordinary upsert under the current definition, so a second run finds every key current and writes
nothing.
"""

from collections.abc import Callable

from pydantic import BaseModel

from chemclaw.core.chem import standard_smiles
from chemclaw.science.fingerprints.molfp.search import record_for
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
    FingerprintStore,
)

#: How one shelved row becomes its current-definition row.
Rebuild = Callable[[FingerprintRecord], FingerprintRecord]


class FingerprintRekeyCounts(BaseModel):
    """What one pass over an index found and wrote — the same numbers previewed or applied."""

    #: Rows read, one per `(source, id)` as the store reports them.
    examined: int = 0
    #: Rows already under the current definition.
    current: int = 0
    #: Shelved rows re-fingerprinted onto a key that had no current row.
    rekeyed: int = 0
    #: Shelved rows whose rebuilt key already had a current row — a salt landing on its free base,
    #: or a row an earlier pass re-keyed.
    already_current: int = 0
    #: Shelved rows whose label no longer parses; left shelved.
    unreadable: int = 0
    #: Of the shelved rows, how many the current standardization gives a different key — the
    #: molecules whose identity the bump moved. Always zero for reactions, keyed by the ELN's id.
    moved: int = 0


def rebuild_molecule(record: FingerprintRecord) -> FingerprintRecord:
    """A molecule row under the current definition: the stored structure standardized again."""
    standard = standard_smiles(record.label)
    return record_for(standard, standard)


def rebuild_reaction(record: FingerprintRecord) -> FingerprintRecord:
    """A reaction row under the current definition, keeping the source half of its key."""
    return record_for_reaction(record.id, record.label).model_copy(update={"source": record.source})


async def rekey_fingerprints(
    store: FingerprintStore, definition: str, rebuild: Rebuild, *, apply: bool
) -> FingerprintRekeyCounts:
    """Re-fingerprint every row of `store` not under `definition`; write only when `apply`.

    The counts are computed before anything is written and identically either way, so a preview's
    numbers are the numbers the apply reports — the property an operator reads a dry run for.

    Args:
        store: The index to walk.
        definition: The current definition, `molecule_definition()` or `reaction_definition()`.
        rebuild: `rebuild_molecule` or `rebuild_reaction`.
        apply: Write the rebuilt rows; otherwise only count them.
    """
    records = await store.all_records()
    counts = FingerprintRekeyCounts(examined=len(records))
    held = {(record.source, record.id) for record in records if record.definition == definition}
    counts.current = len(held)
    rebuilt: list[FingerprintRecord] = []
    for record in records:
        if record.definition == definition:
            continue
        try:
            fresh = rebuild(record)
        except FingerprintError:
            counts.unreadable += 1
            continue
        key = (fresh.source, fresh.id)
        if key != (record.source, record.id):
            counts.moved += 1
        if key in held:
            counts.already_current += 1
            continue
        held.add(key)
        counts.rekeyed += 1
        rebuilt.append(fresh)
    if apply and rebuilt:
        await store.add_many(rebuilt)
    return counts
