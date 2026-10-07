"""The corpus's molecules: similarity over them, and substructure search that does not truncate.

Similarity reuses `PostgresFingerprintStore` pointed at `corpus_molecules`. This module adds that
table's `pattern_bits` column and the screen-then-verify search it enables.

A separate table from `molecule_fingerprints` because it answers a different question and cites
different things: literature precedent citing a patent, not "have we made this" citing a compound
note. Merging would swamp the ELN corpus.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import TupleRow
from rdkit import Chem

from chemclaw.core import db
from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.store import FingerprintError, PostgresFingerprintStore
from chemclaw.science.labels.pattern import (
    compile_query,
    matching,
    pattern_bit_indices,
    query_bit_indices,
)

log = logging.getLogger(__name__)

CORPUS_MOLECULES_TABLE = "corpus_molecules"


def corpus_fingerprints() -> PostgresFingerprintStore:
    """Tanimoto search over the corpus's molecules, on the same class the ELN corpus uses."""
    return PostgresFingerprintStore(
        CORPUS_MOLECULES_TABLE, settings.ecfp_bits, molecule_definition()
    )


class CorpusMolecules:
    """Writes and substructure search over `corpus_molecules`.

    Not a subclass of `PostgresFingerprintStore`: what is left after sharing ranking is a column and
    a search that are not a specialisation of distance ranking.
    """

    _UPSERT = (
        "INSERT INTO corpus_molecules (id, label, bits, definition, pattern_bits) "
        "VALUES (%(id)s, %(label)s, %(bits)s::bit({width}), %(definition)s, %(pattern)s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "label = EXCLUDED.label, bits = EXCLUDED.bits, definition = EXCLUDED.definition, "
        "pattern_bits = EXCLUDED.pattern_bits"
    )

    # `@>` on the GIN index answers "has at least these bits", sound in the direction a prefilter
    # needs: a molecule missing any query bit cannot contain the pattern. Survivors are verified
    # exactly afterwards.
    _SCREEN = (
        "SELECT id FROM corpus_molecules "
        "WHERE pattern_bits @> %(bits)s::integer[] "
        'ORDER BY id COLLATE "C" '
        "LIMIT %(limit)s"
    )

    # A query that sets no bits screens nothing, so every row is a candidate, bounded by the same
    # cap and reported as truncated.
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

        Skipping rather than raising, unlike the ELN path: a patent extract may carry an OCR
        artefact, and refusing the batch would lose the good precedents. The reaction row keeps the
        raw SMILES either way, so a skip costs a missing similarity hit, never a wrong one.
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
        `pattern.matching` decides. Truncation is returned, observed by fetching one row past the
        cap, so a capped negative is never reported as "no precedent".

        `smarts` comes from the model, so the verify runs off the event loop under
        `substructure_match_timeout_seconds`, with the deadline also carried into the worker
        (`_verify_within`) because the timeout cannot stop the thread. The residual is one
        molecule's match.

        Raises:
            FingerprintError: The query is longer than `substructure_query_max_length`, is not
            parseable SMARTS, or its verify outran `substructure_match_timeout_seconds`.
        """
        query = compile_query(smarts)
        bits = query_bit_indices(query)
        # One row past the cap, so truncation is observed rather than inferred.
        over_cap = limit + 1
        sql, params = (
            (self._SCREEN, {"bits": bits, "limit": over_cap})
            if bits
            else (self._ALL, {"limit": over_cap})
        )
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            probed = [str(row[0]) for row in await cur.fetchall()]
        truncated = len(probed) > limit
        candidates = probed[:limit]
        if not bits:
            log.info(
                "substructure query %r sets no pattern bits, so the screen could not narrow the "
                "corpus; %d row(s) were verified directly",
                smarts,
                len(candidates),
            )
        timeout = settings.substructure_match_timeout_seconds
        try:
            # The deadline stops the worker and `wait_for` releases the caller; both raise
            # `TimeoutError`, so the refusal below is the same either way.
            verified = await asyncio.wait_for(
                asyncio.to_thread(_verify_within, candidates, query, time.monotonic() + timeout),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise FingerprintError(
                f"substructure verify for {smarts!r} exceeded {timeout}s over "
                f"{len(candidates)} molecule(s); narrow the pattern "
                "(or raise CHEMCLAW_SUBSTRUCTURE_MATCH_TIMEOUT_SECONDS)"
            ) from exc
        return verified, truncated


class VerifyDeadlineExceeded(TimeoutError):
    """A screen-then-verify pass stopped on its deadline, carrying how many candidates it reached.

    Deliberately not shared with `molfp.ScanDeadlineExceeded`: sharing would add a layering edge for
    one attribute name. A `TimeoutError` subclass so every `except TimeoutError` upstream keeps
    working.
    """

    def __init__(self, reached: int, total: int) -> None:
        """Record how many of `total` candidates were reached."""
        super().__init__(f"substructure verify gave up after {reached} of {total} molecule(s)")
        self.reached = reached
        self.total = total


def _verify_within(structures: Sequence[str], query: Chem.Mol, deadline: float) -> list[str]:
    """`pattern.matching`, one candidate at a time, giving up at `deadline` (the CPU-bound half).

    `asyncio.wait_for` cannot stop the worker thread, so the deadline is read between candidates and
    `VerifyDeadlineExceeded` raised. A wrapper rather than a parameter on `matching`, which stays
    the pure, clock-free containment rule.
    """
    verified: list[str] = []
    for examined, structure in enumerate(structures):
        if time.monotonic() >= deadline:
            raise VerifyDeadlineExceeded(examined, len(structures))
        verified.extend(matching([structure], query))
    return verified
