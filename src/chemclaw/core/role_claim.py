"""A signed statement of the roles a background job was launched with.

A durable activity runs with no token, and a workflow payload is relayed data any writer of
workflow history could edit, so its roles are never taken at face value. At launch the API signs
the actor and roles with `role_claim_secret`; the activity binds the roles only if the signature
checks out against the actor it was handed. A missing key, a missing claim or a mismatch yields no
roles, which every gate treats as holding nothing.

The claim has no expiry: it lives as long as the job it was launched with, and it is bound to the
actor, so it cannot be lifted onto another person's job.
"""

import hashlib
import hmac
from collections.abc import Collection

from chemclaw.core.config import settings

_SEPARATOR = "\x00"


def _digest(actor: str, roles: Collection[str]) -> str:
    """The HMAC-SHA256 of the actor and the sorted roles, hex; empty when no key is configured."""
    key = settings.role_claim_secret.get_secret_value()
    if not key:
        return ""
    message = _SEPARATOR.join([actor, *sorted(set(roles))])
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def sign_roles(actor: str, roles: Collection[str]) -> str:
    """The claim to carry beside `roles` in a job's payload; empty when signing is off."""
    return _digest(actor, roles)


def verified_roles(actor: str, roles: Collection[str], claim: str) -> frozenset[str]:
    """`roles` if `claim` was signed for exactly this actor and these roles, else no roles."""
    expected = _digest(actor, roles)
    if not expected or not claim or not hmac.compare_digest(expected, claim):
        return frozenset()
    return frozenset(roles)
