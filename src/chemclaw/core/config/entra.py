"""Settings for Azure Entra ID identity and authorization.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class EntraSettings(BaseSettings):
    """Azure Entra ID identity and authorization (plan Phase F4, F10-C).

    Grouped because identity is one coherent contract: the OIDC fields, the derived JWKS/issuer
    URLs, the parsed role/action sets, the tool-authz gates, the outbound token endpoint, and
    the enforcement validator that rejects a half-configured deployment — all in one place
    (kernel review note).
    """

    # Front-door auth is OIDC with Entra as IdP: every non-health request carries an Entra JWT
    # validated against the tenant JWKS with the audience checked (confused-deputy guard), and
    # `oid`/`upn` plus app-roles become the `Principal`. True in any real deployment
    # (missing/invalid token is 401); False only for local dev with a stand-in principal.
    # `entra_jwks_url` and `entra_issuer` derive from `entra_tenant_id` when empty.
    entra_required: bool = False
    # Explicit opt-in for a Temporal worker to boot with `entra_required` False
    # (`durable/serve.refuse_unauthenticated_worker`). A worker has no request surface to infer the
    # posture from, and every activity would run as the shared dev principal with gates open, so a
    # deployment that forgot `entra_required` is refused at boot.
    worker_allow_unauthenticated: bool = False
    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_jwks_url: str = ""
    entra_issuer: str = ""
    # Gate for expensive triggers: an action named in `entra_expensive_actions` (comma list, e.g.
    # "sample_conformers,start_bo_campaign") runs only for a user holding a role in
    # `entra_privileged_roles`, so an autonomously planned todo cannot launch costly work outside
    # the user's entitlements. Enforced only under `entra_required`. Both empty by default.
    entra_expensive_actions: str = ""
    entra_privileged_roles: str = ""
    # Per-tool authorization: maps a tool name to the app-roles allowed to call it. A tool with no
    # entry follows `tool_authz_default`: `"deny"` refuses it; `"allow"` permits it except the
    # built-in write gates (`agents.authz.DEFAULT_WRITE_TOOL_GATES`, which need an
    # `entra_privileged_roles` role; an entry here overrides). The write gate only narrows
    # `"allow"`. Enforced only under `entra_required`. JSON in the env, e.g.
    # CHEMCLAW_TOOL_ROLE_GATES='{"sample_conformers": ["process-chemist"]}'. `deny` with no gates
    # blocks every tool, deliberately.
    tool_role_gates: dict[str, list[str]] = Field(default_factory=dict)
    tool_authz_default: Literal["allow", "deny"] = "allow"
    # Identity a user-triggered workflow records with no authenticated user: local dev and
    # system-triggered jobs only; under enforcement `require_actor` rejects an absent user.
    service_actor_id: str = "service-account"
    # How long the front door waits on the tenant when fetching keys (`api/auth._client_for` builds
    # the `PyJWKClient` with it), instead of PyJWT's 30-second default.
    entra_http_timeout_seconds: float = Field(default=10.0, gt=0)
    # PEM bundle of CAs the tenant's JWKS endpoint is verified against, replacing certifi (empty
    # means certifi). For a test tenant behind a private CA or a TLS-inspecting proxy. There is no
    # way to switch verification off: an unverified key set is a forgeable tenant. The front door
    # refuses to boot on a missing or certificate-less path
    # (`api/auth.refuse_unusable_entra_ca_bundle`).
    entra_ca_bundle: str = ""
    # Minimum gap between JWKS re-fetches forced by an unknown `kid`. The `kid` is caller-chosen, so
    # without a floor each unauthenticated request could cost an outbound IdP fetch. A new signing
    # key is picked up at most this late.
    entra_jwks_refresh_cooldown_seconds: float = Field(default=60.0, ge=0)
    # How long a failed JWKS fetch is remembered before asking the tenant again, so an IdP outage
    # costs one fetch per window per process rather than one per request. 0 disables.
    entra_jwks_failure_backoff_seconds: float = Field(default=5.0, ge=0)
    # Fold the token's `groups` claim into the one role set every gate matches on, so an AD security
    # group is an entitlement like any role. Each value is namespaced with
    # `core.identity_context.GROUP_ROLE_PREFIX` (`group:<value>`), so a group cannot be read as the
    # app role of the same name. Off by default because the tenant must emit the optional claim; a
    # group assigned to an app role already arrives in `roles`.
    entra_group_claims_as_roles: bool = False

    @property
    def entra_expensive_action_set(self) -> frozenset[str]:
        """The actions that require a privileged role (parsed comma list)."""
        return frozenset(a.strip() for a in self.entra_expensive_actions.split(",") if a.strip())

    @property
    def entra_privileged_role_set(self) -> frozenset[str]:
        """The roles that authorize an expensive action (parsed comma list)."""
        return frozenset(r.strip() for r in self.entra_privileged_roles.split(",") if r.strip())

    @property
    def entra_jwks_endpoint(self) -> str:
        """The JWKS URL: explicit override, else the tenant's standard v2.0 keys endpoint."""
        if self.entra_jwks_url:
            return self.entra_jwks_url
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def entra_issuer_url(self) -> str:
        """The token issuer: explicit override, else the tenant's standard v2.0 issuer."""
        if self.entra_issuer:
            return self.entra_issuer
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"

    @model_validator(mode="after")
    def _entra_enforcement_is_configured(self) -> Self:
        """Under `entra_required`, fail fast on a half-configured identity setup.

        Two mistakes that would otherwise surface as request-time deny-alls:
        - an empty `entra_audience`, or no tenant/issuer/JWKS source (issuer and JWKS derive
          independently, so each needs one), rejects every token;
        - an expensive action with no privileged role refuses that action for everyone, since
          `authz.authorize_trigger` fails closed on an empty role set.

        Roles without actions is valid and normal: the action set derives from manifests'
        `expensive: true` (`authz.expensive_actions`).
        """
        if not self.entra_required:
            return self
        if not self.entra_audience:
            raise ValueError("entra_audience must be set when entra_required")
        if not (self.entra_tenant_id or self.entra_issuer):
            raise ValueError("entra_tenant_id or entra_issuer must be set when entra_required")
        if not (self.entra_tenant_id or self.entra_jwks_url):
            raise ValueError(
                "entra_tenant_id or entra_jwks_url must be set when entra_required "
                "(the issuer alone cannot resolve the JWKS keys endpoint)"
            )
        if self.entra_expensive_actions and not self.entra_privileged_roles:
            raise ValueError(
                "entra_expensive_actions needs entra_privileged_roles: naming a gated action "
                "with no privileged role refuses it to every user, since the trigger gate fails "
                "closed on an empty role set. The reverse is fine — entra_privileged_roles alone "
                "is the normal setup, because the expensive set derives from the connector "
                "manifests"
            )
        return self
