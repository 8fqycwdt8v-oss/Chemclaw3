"""Keys a tenant did not mint: two tenants write the same one, and both rows exist.

A content hash, a name a data source carries, a person and a note slug are all things two tenants
can both produce. With the policies on, the second tenant's row cannot see the first's, so a key
that does not carry the tenant either refuses its write or an `ON CONFLICT DO NOTHING` drops it.
Each case here writes the same key from two tenants through the store that owns it, as a role the
policies bind, and counts the rows each tenant holds afterwards.
"""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from chemclaw.agent import subscriptions
from chemclaw.agent.behaviour_proposals import PostgresProposalStore, Proposal
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.tenancy import bound_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from chemclaw.ingest.commitments.models import Commitment
from chemclaw.ingest.commitments.store import record_commitments
from chemclaw.ingest.documents.index import ChunkRecord, FileRecord, PostgresDocumentIndex
from chemclaw.ingest.eln.cursor import store_cursor
from chemclaw.ingest.eln.records import PostgresReactionRecordStore
from chemclaw.ingest.labels.cursor import store_corpus_cursor
from chemclaw.ingest.rejections import record_refusals
from chemclaw.science.fingerprints.store import FingerprintRecord, PostgresFingerprintStore
from chemclaw.science.labels.store import PostgresLabelIndex
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database
from tests.test_label_index import _label


@pytest.fixture
async def two_tenants(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Tenancy on, keys widened, acme and globex registered, the application as a bound role."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    forget_registered_tenants()
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        yield database


async def _counts(database: TenancyDatabase, table: str) -> dict[str, int]:
    """How many rows each tenant holds in `table`, read as the superuser."""
    async with await connect(database.owner_dsn) as conn:
        cursor = await conn.execute(
            f"SELECT tenant_id, count(*) FROM {table} WHERE tenant_id <> 'default' "
            "GROUP BY 1 ORDER BY 1"
        )
        return {str(row[0]): int(row[1]) for row in await cursor.fetchall()}


async def _eln_cursor() -> None:
    await store_cursor("eln-json", datetime(2026, 1, 1, tzinfo=UTC))


async def _corpus_cursor() -> None:
    await store_corpus_cursor("corpus", "after-1")


async def _rejection() -> None:
    await record_refusals("eln-json", {"entry-1": "a reason"})


async def _commitment() -> None:
    await record_commitments(
        [Commitment(source="commitments-json", external_id="PRJ-1", title="t")]
    )


async def _subscription() -> None:
    await subscriptions.add("person-1", "suzuki", None)


async def _proposal() -> None:
    body = "content"
    await PostgresProposalStore().propose(
        Proposal(
            kind="profile",
            name="p",
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            content=body,
            rationale="r",
            actor="person-1",
            session_id="",
            correlation_id="",
        )
    )


async def _document() -> None:
    chunking = "2000:200"
    file = FileRecord(
        path="a.txt",
        source="sharedrive",
        doc_id="doc-hash-1",
        fingerprint="1:1",
        chunking_key=chunking,
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    chunk = ChunkRecord(
        doc_id="doc-hash-1",
        chunking_key=chunking,
        ordinal=0,
        content="text",
        embedding=[0.1] * settings.embedding_dim,
    )
    await PostgresDocumentIndex().upsert([file], [chunk], "test:key:d1536:m")


async def _reaction_record() -> None:
    from chemclaw.ingest.eln.records import ReactionRecord

    await PostgresReactionRecordStore().record(
        [ReactionRecord(reaction_id="EXP-1", body="body", source="eln-json")], "eln-json"
    )


async def _label_row() -> None:
    await PostgresLabelIndex().record(_label("r1"))


async def _design() -> None:
    """The store's own upsert, with the id a person's ask hashes to: the same in both tenants."""
    from chemclaw.core import db
    from chemclaw.protocols.store import _UPSERT_DESIGN

    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            _UPSERT_DESIGN,
            {
                "design_id": "design-abc123",
                "title": "t",
                "mode": "screen",
                "status": "draft",
                "project": "",
                "opened_by": "person-1",
                "session_id": "",
                "correlation_id": "",
                "revision": 1,
                "arm_count": 0,
                "blocker_count": 0,
            },
        )
        await conn.commit()


async def _corpus_reaction_fingerprint() -> None:
    store = PostgresFingerprintStore(
        "corpus_reactions", settings.drfp_bits, "drfp:test", source_keyed=True
    )
    await store.add(
        FingerprintRecord(
            id="r1",
            label="CC>>CO",
            bits="0" * settings.drfp_bits,
            definition="drfp:test",
            source="s",
        )
    )


async def _reaction_fingerprint() -> None:
    store = PostgresFingerprintStore(
        "reaction_fingerprints", settings.drfp_bits, "drfp:test", source_keyed=True
    )
    await store.add(
        FingerprintRecord(
            id="r1",
            label="CC>>CO",
            bits="0" * settings.drfp_bits,
            definition="drfp:test",
            source="s",
        )
    )


_CASES: list[tuple[str, Callable[[], Awaitable[None]]]] = [
    ("sync_cursors", _eln_cursor),
    ("corpus_cursors", _corpus_cursor),
    ("ingest_rejections", _rejection),
    ("commitments", _commitment),
    ("subscriptions", _subscription),
    ("behaviour_proposals", _proposal),
    ("document_files", _document),
    ("document_chunks", _document),
    ("reaction_records", _reaction_record),
    ("reaction_labels", _label_row),
    ("reaction_species", _label_row),
    ("reaction_fingerprints", _reaction_fingerprint),
    ("corpus_reactions", _corpus_reaction_fingerprint),
    ("experiment_protocols", _design),
]


@pytest.mark.parametrize(("table", "write"), _CASES, ids=[case[0] for case in _CASES])
async def test_two_tenants_write_the_same_key_and_both_rows_exist(
    table: str, write: Callable[[], Awaitable[None]], two_tenants: TenancyDatabase
) -> None:
    """The second tenant's write is neither refused nor dropped by the first tenant's row."""
    with bound_tenant("acme"):
        await write()
    with bound_tenant("globex"):
        await write()
    counts = await _counts(two_tenants, table)
    assert counts.get("acme", 0) >= 1 and counts.get("globex", 0) >= 1, (
        f"{table}: the same key written by two tenants left {counts}"
    )
    assert counts["acme"] == counts["globex"], f"{table}: {counts}"


async def test_a_tenant_repeating_its_own_write_still_updates_in_place(
    two_tenants: TenancyDatabase,
) -> None:
    """The key still identifies a row within one tenant: a repeat is an update, not a second row."""
    with bound_tenant("acme"):
        await _corpus_cursor()
        await _corpus_cursor()
        await _rejection()
        await _rejection()
    assert (await _counts(two_tenants, "corpus_cursors")).get("acme") == 1
    assert (await _counts(two_tenants, "ingest_rejections")).get("acme") == 1
