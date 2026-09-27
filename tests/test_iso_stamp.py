"""`core.db.IsoStamp` spells a `TIMESTAMPTZ` column the way every seam reading it always has.

Three models (`durable/pending_store`, `durable/effect_ledger`, `operations/evidence_pack`) each
carried an identical private copy of this validator, each saying the spelling is a wire contract.
One definition now, so the contract is held here once rather than trusted three times.
"""

from datetime import UTC, datetime

from pydantic import BaseModel

from chemclaw.core.db import IsoStamp
from chemclaw.durable.effect_ledger import EffectRecord
from chemclaw.durable.pending_store import PendingRequest
from chemclaw.operations.evidence_pack import ToolCall


class _Row(BaseModel):
    at: IsoStamp = ""


def test_a_datetime_is_isoformat_a_null_is_empty_and_a_string_passes() -> None:
    """`isoformat()` rather than Postgres' `::text` spelling; NULL reads as "never", not `None`."""
    instant = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    assert _Row(at=instant).at == "2026-09-16T10:00:00+00:00"  # type: ignore[arg-type]
    assert _Row(at=None).at == ""  # type: ignore[arg-type]
    assert _Row(at="already a string").at == "already a string"


def test_the_three_seams_share_the_one_definition() -> None:
    """Each model's stamp fields resolve to `IsoStamp`, so none can drift to its own spelling."""
    shared = _Row.model_fields["at"].metadata
    assert EffectRecord.model_fields["settled_at"].metadata == shared
    assert PendingRequest.model_fields["answered_at"].metadata == shared
    assert ToolCall.model_fields["at"].metadata == shared
