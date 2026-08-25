"""What a republish job takes — the request half of this bundle's durable contract.

Its own module, leaf by construction: `connectors/jobs.py` resolves `params_model` by importing it
*inside the chat service*, so anything reachable from here is loaded there too. Keeping it to
pydantic and nothing else is what `tests/test_connector_isolation.py` enforces, and it is the same
split `connectors/calc/specs.py` was carved out to hold.
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
