"""Caller identity for Phase-6 RBAC (plan steps 6.1–6.3).

A `Principal` is the validated identity of the human on whose behalf the agent acts: the
Entra ID object id (`oid`) and UPN, plus the app-roles and group ids carried in the token's
claims. Phase 6 obtains it by validating an Entra JWT against the tenant JWKS — a live step
that needs the real tenant (see `SECURITY.md`); this module models the identity itself and the
*pure* things derived from it — the audit `actor`, and (via `agents.skill_access`) which skills
are visible — both fully testable offline with a synthetic principal.

Anonymous / dev use passes no principal at all, which preserves today's behavior: the audit
`actor` stays `"unknown"` and (with no role gates configured) every skill stays visible.
"""

from pydantic import BaseModel, ConfigDict


class Principal(BaseModel):
    """The validated caller identity behind a conversation.

    Immutable (`frozen`) because an identity must not be mutated after validation — a tool or
    middleware that received a principal cannot quietly change who it is acting as.
    """

    model_config = ConfigDict(frozen=True)

    # Entra object id — the stable, non-reassignable per-user id. This is what the audit trail
    # records (see `actor`), never the UPN, which can be renamed.
    oid: str
    # User principal name (e.g. email); human-readable, but not a stable key — audit on `oid`.
    upn: str | None = None
    # App-roles and group object-ids from the token claims; the inputs to authorization
    # decisions (which skills are visible, later which tools may run).
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()

    @property
    def actor(self) -> str:
        """The stable id recorded in the GxP audit trail — the Entra object id."""
        return self.oid
