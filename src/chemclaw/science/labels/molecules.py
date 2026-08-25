"""The corpus's molecules: similarity over them, and substructure search that does not truncate.

`corpus_molecules` carries the same five columns `molecule_fingerprints` does, so similarity needs
no code at all — `PostgresFingerprintStore` is already table-parameterised and
`corpus_fingerprints()` just points it at the other table. What this module adds is the column that
table has and the other does not: `pattern_bits`, and the screen-then-verify search it makes
possible.

**Why a second table rather than more rows in the first.** They answer different questions and cite
different things: `molecule_fingerprints` is "have we made this?" and its hits cite a compound note,
this is "is there literature precedent?" and its hits cite a patent. Merging them would swamp the
ELN corpus by four orders of magnitude and hand `similar_molecules` millions of hits whose
`compound_note_id` resolves to nothing.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.store import PostgresFingerprintStore
from chemclaw.science.labels.pattern import contains, pattern_bit_indices, query_bit_indices

log = logging.getLogger(__name__)

CORPUS_MOLECULES_TABLE = "corpus_molecules"


def corpus_fingerprints() -> PostgresFingerprintStore:
    """Tanimoto search over the corpus's molecules, on the same class the ELN corpus uses."""
    return PostgresFingerprintStore(
        CORPUS_MOLECULES_TABLE, settings.ecfp_bits, molecule_definition()
    )


class CorpusMolecules:
    """Writes and substructure search over `corpus_molecules`.

    Separate from `PostgresFingerprintStore` rather than a subclass of it: the shared half is
    already shared by pointing that class at this table, and what is left — a column it does not
    know about and a search it cannot express — is not a specialisation of ranking by distance.
    """

    _UPSERT = (
        "INSERT INTO corpus_molecules (id, label, bits, definition, pattern_bits) "
        "VALUES (%(id)s, %(label)s, %(bits)s::bit({width}), %(definition)s, %(pattern)s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "label = EXCLUDED.label, bits = EXCLUDED.bits, definition = EXCLUDED.definition, "
        "pattern_bits = EXCLUDED.pattern_bits"
    )

    # The screen. `@>` on the GIN index answers "has at least these bits", which is sound in the
    # one direction a prefilter needs: a molecule missing any of the query's bits provably cannot
    # contain the pattern. The survivors are verified exactly in Python afterwards.
    _SCREEN = (
        "SELECT id FROM corpus_molecules "
        "WHERE pattern_bits @> %(bits)s::integer[] "
        'ORDER BY id COLLATE "C" '
        "LIMIT %(limit)s"
    )

    # The degenerate case: a query so generic it sets no bits screens nothing, so there is nothing
    # to intersect with and every row is a candidate. Bounded by the same cap, and the caller is
    # told the scan truncated — a search that silently examined the first n rows of a corpus and
    # reported no hits is the defect `FingerprintSearch.scan_truncated` exists to name.
    _ALL = 'SELECT id FROM corpus_molecules ORDER BY id COLLATE "C" LIMIT %(limit)s'

    def __init__(self, dsn: str | None = None) -> None:
        """Bind to the configured DSN (or an explicit one, for tests against a scratch database)."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        self._upsert = self._UPSERT.format(width=settings.ecfp_bits)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a bounded connection from the shared pool."""
        async with db.connection(self._dsn) as conn:
            yield conn

    async def add_many(self, smiles: Sequence[str]) -> int:
        """Index each distinct structure, skipping the ones RDKit cannot read.

        Skipping rather than raising, and the asymmetry with the ELN path is deliberate: a patent
        extract's fiftieth species may be an OCR artefact, and refusing the batch over it would
        lose forty-nine good precedents. The reaction row is written either way — the species keeps
        its raw SMILES there — so what a skip costs is a missing similarity hit, never a wrong one.
        """
        written = 0
        skipped = 0
        async with self._connection() as conn:
            for structure in dict.fromkeys(smiles):
                try:
                    params = {
                        "id": structure,
                        "label": structure,
                        "bits": ecfp_bitstring(structure),
                        "definition": molecule_definition(),
                        "pattern": pattern_bit_indices(structure),
                    }
                except (InvalidSmilesError, ValueError):
                    skipped += 1
                    continue
                await conn.execute(self._upsert, params)
                written += 1
            await conn.commit()
        if skipped:
            log.info("corpus molecules: %d unparseable structure(s) not indexed", skipped)
        return written

    async def containing(self, smarts: str, limit: int) -> tuple[list[str], bool]:
        """Structures that genuinely contain `smarts`, and whether the screen was truncated.

        Screen then verify: the GIN containment test admits every true hit and some false ones, and
        `pattern.contains` decides. Truncation is reported rather than logged, because a caller that
        cannot tell a complete negative from a capped one will report "no precedent exists" for a
        corpus whose one match it never looked at.
        """
        bits = query_bit_indices(smarts)
        sql, params = (
            (self._SCREEN, {"bits": bits, "limit": limit})
            if bits
            else (self._ALL, {"limit": limit})
        )
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            candidates = [str(row[0]) for row in await cur.fetchall()]
        if not bits:
            log.info(
                "substructure query %r sets no pattern bits, so the screen could not narrow the "
                "corpus; %d row(s) were verified directly",
                smarts,
                len(candidates),
            )
        return [c for c in candidates if contains(c, smarts)], len(candidates) == limit
