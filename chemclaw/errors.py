"""Common base for Chemclaw's bad-data errors.

Why this exists: every layer defines its own bad-input error (fingerprints, ELN
mapping, ingestion, metrics, playbooks). Before this base they were five parallel
`ValueError` subclasses, and every reject-and-continue boundary had to enumerate
the exact types — forgetting one turned a single bad record into a batch-aborting
poison pill (the CHECKMATE-review sync bug). Deriving them all from `ChemclawError`
makes "this input is bad, skip it and move on" one catchable contract.

It stays a `ValueError` subclass so Temporal retry policies that mark `ValueError`
non-retryable keep treating bad data as a fast failure, never a retry loop.
"""


class ChemclawError(ValueError):
    """Base for all domain errors meaning "this input/data is invalid".

    Catch this at batch boundaries (reject-and-continue); raise a specific
    subclass at the point of failure so messages stay layer-accurate.
    """
