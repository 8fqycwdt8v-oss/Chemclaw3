"""Chemclaw's two cross-cutting error contracts: bad data, and an unreachable subsystem.

**`ChemclawError` — "this input/data is invalid".** Every layer defines its own bad-input error
(fingerprints, ELN mapping, ingestion, metrics, playbooks). Before this base they were five
parallel `ValueError` subclasses, and every reject-and-continue boundary had to enumerate the
exact types — forgetting one turned a single bad record into a batch-aborting poison pill (the
CHECKMATE-review sync bug). Deriving them all from `ChemclawError` makes "this input is bad, skip
it and move on" one catchable contract.

It stays a `ValueError` subclass so in-process `except ValueError` boundaries keep
catching bad data. Temporal, however, matches `non_retryable_error_types` by exact
class-name string — NOT by isinstance — so subclassing alone does not make an error
non-retryable across an activity boundary: every concrete subclass name must also be
registered in `chemclaw.durable.publish._BAD_DATA_TYPES` (a completeness test in
`tests/test_publish.py` fails when one is forgotten).

**`SubsystemUnavailableError` — "the infrastructure this needs is not answering".** The exact
opposite claim about the same call, and therefore deliberately *outside* the hierarchy above (see
its own docstring). Both live here because both are contracts the whole tree raises and catches,
and because the one middleware that shows either of them to the model
(`chemclaw.agent.tool_authz.surface_domain_errors`) must be able to import both without reaching
into a subsystem's own module.
"""


class ChemclawError(ValueError):
    """Base for all domain errors meaning "this input/data is invalid".

    Catch this at batch boundaries (reject-and-continue); raise a specific
    subclass at the point of failure so messages stay layer-accurate. When a new
    subclass can cross a Temporal activity boundary, add its class name to
    `chemclaw.durable.publish._BAD_DATA_TYPES` — Temporal matches non-retryable types
    by exact name, so the hierarchy alone does not cover it.
    """


class SubsystemUnavailableError(Exception):
    """An infrastructure dependency could not be reached, so the requested work never began.

    The message is written for the **chemist**, because
    `chemclaw.agent.tool_authz.surface_domain_errors` hands it to the model verbatim as the tool's
    result: it names the subsystem, says what the caller has lost (a durable job cannot be started
    right now), and says plainly that this is an outage rather than a problem with what they asked.
    Raisers therefore keep it free of hostnames, ports and driver text — the underlying exception
    carries all of that as `__cause__`, for the log and the operator.

    Why it exists at all: an unreachable Temporal broker reached the model as MAF's opaque
    "Error: Function failed.", and in the 2026-08-03 live run the model responded to that by
    **writing the entire development report by hand** — tables, executive summary, numbers,
    citations — and presenting it as having entered the PR-gate. The generator never ran. An error
    that says nothing is not a neutral outcome; it is an invitation to invent one.

    Deliberately **not** a `ChemclawError` (hence not a `ValueError`), for the reason
    `chemclaw.agent.authz.AuthorizationError` is not one, applied to the opposite claim: that
    hierarchy means "this input/data is invalid", and an outage says nothing about the data — the
    identical call with the identical arguments succeeds once the subsystem is back. Concretely,
    `ChemclawError` is the **non-retryable** contract (every subclass name is registered in
    `chemclaw.durable.publish._BAD_DATA_TYPES`), and an unreachable broker is the textbook
    *retryable* failure, so membership would be wrong twice over: it would tell Temporal to fail
    an activity fast on precisely the fault a retry fixes. `tests/test_publish.py` asserts this
    class's absence from that list on purpose, so a future completeness sweep cannot quietly add
    it.
    """
