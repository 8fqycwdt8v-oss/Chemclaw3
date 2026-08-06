"""Postgres backend for the artifact store (D-124).

Implements the same `ArtifactStore` interface as `InMemoryArtifactStore`, backed by the
`artifact_blobs` + `calculation_artifacts` tables (`infra/sql/019_artifact_store.sql`), so a
calculation's by-products survive process restarts and are shared across workers.

Storage is `BYTEA`, not an object store. The artifacts this system actually produces are kilobytes
to a few megabytes — a 76-atom Turbomole `hessian` is single-digit MB of text and roughly a fifth
of that once deflated — and Postgres is the only durable store the deployment already has. An
S3-compatible bucket would add an infrastructure dependency, a fourth secret to the three-secret
model, and a bucket-endpoint host literal that muddies `tests/test_no_egress.py`; a shared
filesystem CAS would need an RWX volume no OpenShift storage class guarantees, since the service
and the workers are separate pods. The `ArtifactStore` Protocol is the seam that lets a DFT-scale
deployment add one later without touching a caller.

A write is two statements: the blob is inserted by content address (a no-op when some other
calculation already stored those exact bytes) and the link row is upserted. Like `PostgresStore`,
connections are short-lived and borrowed through `chemclaw.core.db.connection`.
"""

import logging

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import (
    ArtifactRef,
    ArtifactStore,
    content_address,
    decode,
    encode,
    too_large,
)

logger = logging.getLogger(__name__)

# The blob is keyed by its content address, so a second calculation producing identical bytes
# stores nothing and simply links to what is there. `DO NOTHING` rather than `DO UPDATE`: the
# content address *is* the content, so there is never anything to update.
_INSERT_BLOB = """
    INSERT INTO artifact_blobs (content_hash, codec, byte_size, stored_bytes, data)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (content_hash) DO NOTHING
"""

_UPSERT_LINK = """
    INSERT INTO calculation_artifacts
        (calc_key, name, content_hash, media_type, compute_seconds)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (calc_key, name) DO UPDATE SET
        content_hash = EXCLUDED.content_hash,
        media_type = EXCLUDED.media_type,
        compute_seconds = COALESCE(EXCLUDED.compute_seconds, calculation_artifacts.compute_seconds),
        created_at = now()
"""

_SELECT_BLOB = "SELECT codec, data FROM artifact_blobs WHERE content_hash = %s"

# Refresh the access stamp only when it is already stale, so a read on the reuse hot path is a
# read. The predicate does the deciding in SQL, which keeps it to one round trip.
_TOUCH_BLOB = """
    UPDATE artifact_blobs SET last_access_at = now()
    WHERE content_hash = %s AND last_access_at < now() - make_interval(secs => %s)
"""

_SELECT_LINKS = """
    SELECT a.name, a.content_hash, a.media_type, b.byte_size
    FROM calculation_artifacts AS a
    JOIN artifact_blobs AS b ON b.content_hash = a.content_hash
    WHERE a.calc_key = %s
    ORDER BY a.name
"""


class PostgresArtifactStore:
    """Durable `ArtifactStore` backed by Postgres.

    Opens a short-lived connection per call for the same reason `PostgresStore` does: artifact
    traffic is coarse-grained relative to the calculations that generate it, and the process-wide
    pool underneath `chemclaw.core.db.connection` already removes the handshake where it matters.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Use the given DSN, or the configured one by default."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    async def put(
        self,
        calc_key: str,
        name: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        compute_seconds: float | None = None,
    ) -> ArtifactRef | None:
        """Store `data` under `(calc_key, name)`; return its ref, or `None` if it was not stored.

        Refuses — returning `None`, never raising — when the store is disabled or the payload
        exceeds `artifact_max_bytes`. That is the whole of the "an artifact is optional" contract
        on the write side.
        """
        if not settings.artifact_store_enabled or too_large(len(data)):
            return None
        digest = content_address(data)
        codec, payload = encode(data)
        async with db.bounded(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT_BLOB, (digest, codec, len(data), len(payload), payload))
                await cur.execute(
                    _UPSERT_LINK, (calc_key, name, digest, media_type, compute_seconds)
                )
            await conn.commit()
        return ArtifactRef(
            calc_key=calc_key,
            name=name,
            content_hash=digest,
            byte_size=len(data),
            media_type=media_type,
        )

    async def open(self, content_hash: str) -> bytes | None:
        """Return the artifact's original bytes, or `None` on a miss."""
        async with db.bounded(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_BLOB, (content_hash,))
                row = await cur.fetchone()
                if row is None:
                    return None
                codec, data = row
                await cur.execute(
                    _TOUCH_BLOB, (content_hash, settings.artifact_access_stamp_seconds)
                )
            await conn.commit()
        # psycopg may hand BYTEA back as a memoryview; the codec layer wants real bytes.
        return decode(codec, bytes(data))

    async def list_for(self, calc_key: str) -> list[ArtifactRef]:
        """Return every artifact this calculation produced, ordered by name."""
        async with db.bounded(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_LINKS, (calc_key,))
                rows = await cur.fetchall()
        return [
            ArtifactRef(
                calc_key=calc_key,
                name=name,
                content_hash=content_hash,
                byte_size=byte_size,
                media_type=media_type,
            )
            for name, content_hash, media_type, byte_size in rows
        ]


def default_artifact_store() -> ArtifactStore:
    """Return the production artifact store.

    The one place that names the production backend, mirroring
    `chemclaw.science.calc.postgres_store.default_store`
    so a calculator module does not have to know which one it is. Tests swap it at the importing
    module (`monkeypatch.setattr(<module>, "default_artifact_store", ...)`).
    """
    return PostgresArtifactStore()
