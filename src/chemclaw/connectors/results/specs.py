"""What a republish job takes: the request half of this bundle's durable contract.

A leaf module: `connectors/jobs.py` imports `params_model` inside the chat service, so this imports
pydantic only (`tests/test_connector_isolation.py`).
"""

from pydantic import BaseModel, ConfigDict, Field


class RepublishSpec(BaseModel):
    """A request to re-queue stored calculations for the external results store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requeue_failed: bool = Field(
        default=False,
        description=(
            "Also return publications that exhausted their retry budget to the queue. Use this "
            "once the reason a destination was refusing deliveries has been fixed — a retired row "
            "is kept precisely so it can be re-sent rather than re-derived."
        ),
    )
    batch: int = Field(
        default=500,
        gt=0,
        le=5000,
        description=(
            "How many stored rows to read per round trip. The default suits a corpus of any size; "
            "lower it only if the database is under pressure."
        ),
    )
