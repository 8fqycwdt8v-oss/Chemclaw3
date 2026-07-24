"""Common base for Chemclaw's bad-data errors.

Why this exists: every layer defines its own bad-input error (fingerprints, ELN
mapping, ingestion, metrics, playbooks). Before this base they were five parallel
`ValueError` subclasses, and every reject-and-continue boundary had to enumerate
the exact types — forgetting one turned a single bad record into a batch-aborting
poison pill (the CHECKMATE-review sync bug). Deriving them all from `ChemclawError`
makes "this input is bad, skip it and move on" one catchable contract.

It stays a `ValueError` subclass so in-process `except ValueError` boundaries keep
catching bad data. Temporal, however, matches `non_retryable_error_types` by exact
class-name string — NOT by isinstance — so subclassing alone does not make an error
non-retryable across an activity boundary: every concrete subclass name must also be
registered in `workflows.publish._BAD_DATA_TYPES` (a completeness test in
`tests/test_publish.py` fails when one is forgotten).
"""


class ChemclawError(ValueError):
    """Base for all domain errors meaning "this input/data is invalid".

    Catch this at batch boundaries (reject-and-continue); raise a specific
    subclass at the point of failure so messages stay layer-accurate. When a new
    subclass can cross a Temporal activity boundary, add its class name to
    `workflows.publish._BAD_DATA_TYPES` — Temporal matches non-retryable types
    by exact name, so the hierarchy alone does not cover it.
    """
