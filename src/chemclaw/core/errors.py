"""Chemclaw's two cross-cutting error contracts: bad data, and an unreachable subsystem.

`ChemclawError` means "this input/data is invalid": every layer's bad-input error derives from it,
so a reject-and-continue boundary catches one type. It stays a `ValueError`. Temporal matches
`non_retryable_error_types` by exact class name, so every concrete subclass that can cross an
activity boundary must also be listed in `chemclaw.durable.publish._BAD_DATA_TYPES`.

`SubsystemUnavailableError` means "the infrastructure this needs is not answering" and sits
deliberately outside that hierarchy. Both live here so `surface_domain_errors` can import them
without reaching into a subsystem.
"""

from typing import ClassVar


class ChemclawError(ValueError):
    """Base for all domain errors meaning "this input/data is invalid".

    Catch at batch boundaries (reject-and-continue); raise a specific subclass at the point of
    failure. A subclass that can cross a Temporal activity boundary must be added to
    `chemclaw.durable.publish._BAD_DATA_TYPES`, which Temporal matches by exact name.
    """


class SubsystemUnavailableError(Exception):
    """An infrastructure dependency could not be reached, so the requested work never began.

    The message is written for the chemist, because `surface_domain_errors` hands it to the model
    verbatim: name the subsystem, say what is lost, and say it is an outage, not a problem with the
    request. Keep hostnames, ports and driver text out of it; they travel on `__cause__`. An opaque
    error invites the model to fabricate the result instead.

    Not a `ChemclawError`: an outage is retryable and says nothing about the data, while
    `ChemclawError` is the non-retryable contract. It must stay out of `_BAD_DATA_TYPES`.
    """


class AtCapacityError(SubsystemUnavailableError):
    """A backend was reached, ran nothing, and refused because every slot it has was already held.

    Retryable soon: the identical call succeeds once admitted work finishes. A subclass of
    `SubsystemUnavailableError` so every outage retry contract applies, and its own class so
    `connectors/server.py::_sanitize_tool_errors` can prefix `marker` — the fleet's at-capacity
    format (`core/mcp_session.at_capacity`) — letting callers queue instead of failing.
    """

    #: The server whose slots were full — the `<server>` in `[<server>-at-capacity]`.
    server: ClassVar[str] = ""

    @property
    def marker(self) -> str:
        """This refusal's at-capacity token, in the fleet's one format."""
        return f"[{self.server}-at-capacity]"
