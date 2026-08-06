"""Azure Entra ID identity and authorization (plan Phase F4, F10-C).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class EntraSettings(BaseSettings):
    """Azure Entra ID identity and authorization (plan Phase F4, F10-C).

    Grouped because identity is one coherent contract: the OIDC fields, the derived JWKS/issuer
    URLs, the parsed role/action sets, the tool-authz gates, the workload-federation/OBO
    bridges, and the enforcement validator that rejects a half-configured deployment — all in
    one place (kernel review note).
    """

    # User auth at the front door is OIDC with Entra as the IdP: the service is an Entra app
    # registration, and every non-health request carries an Entra JWT that is validated against
    # the tenant JWKS with the audience checked (the confused-deputy guard — the service is both
    # OAuth client and resource). `oid`/`upn` + app-roles are extracted into a `Principal` that
    # authorizes and attributes every backend action. `entra_required` gates enforcement: True
    # in any real deployment (a missing/invalid token is 401); False only for local dev, where a
    # stand-in principal runs the app without a tenant. `entra_jwks_url`/`entra_issuer` default
    # empty and derive from `entra_tenant_id` when set (the standard v2.0 endpoints), so a
    # deployment sets just tenant + audience + required.
    entra_required: bool = False
    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_jwks_url: str = ""
    entra_issuer: str = ""
    # Authorization for expensive triggers (plan F4-T5): the single fachliche gate. An action
    # named in `entra_expensive_actions` (comma list, e.g. "compute_dft_energy,start_bo_campaign")
    # may run only for a user holding at least one role in `entra_privileged_roles` — so an
    # autonomously-planned todo cannot launch a costly HPC/BO job outside the requesting user's
    # entitlements. Enforced only when `entra_required` (a real deployment with real roles); in
    # dev the gate is open. Both empty by default: nothing is privileged until a deployment
    # declares it.
    entra_expensive_actions: str = ""
    entra_privileged_roles: str = ""
    # Per-tool authorization (plan F10-C): generalizes the single expensive-trigger gate to
    # *every* tool invocation via one middleware. `tool_role_gates` maps a tool name to the
    # Entra app-roles allowed to call it. A tool with no entry follows `tool_authz_default`:
    # under `"deny"` (allowlist mode) it is refused outright — only listed tools are callable,
    # by a role-holder; under `"allow"` it is callable, except the built-in write-tool gates
    # (`agent.authz.default_write_tool_gates()`: job launchers and shared-state writes require
    # an `entra_privileged_roles` role out of the box — an explicit entry here overrides that).
    # The built-in write gate only narrows `"allow"`; it never widens `"deny"`. Enforced only
    # when `entra_required` (dev gate is open). ENV override for the gates is JSON, e.g.
    # CHEMCLAW_TOOL_ROLE_GATES='{"compute_dft_energy": ["process-chemist"]}'. Note: `deny` with an
    # empty `tool_role_gates` blocks *all* tools — a deliberate lockdown, not a footgun to
    # stumble into.
    tool_role_gates: dict[str, list[str]] = Field(default_factory=dict)
    tool_authz_default: Literal["allow", "deny"] = "allow"
    # The identity a *user-triggered* workflow records when there is no authenticated user (plan
    # F4-T3). Only reachable in local dev (`entra_required=False`, no tenant) and for
    # system-triggered jobs; under enforcement `require_actor` rejects an absent user instead of
    # falling back. Config, not the old magic `"unknown"` literal.
    service_actor_id: str = "service-account"
    # Workload identity federation (plan F4-T2): a backend pod mints its *own* short-lived Entra
    # token by exchanging its projected ServiceAccount JWT (at `entra_sa_token_path`) via the
    # OAuth2 client-credentials grant with a `client_assertion` — no client secret ever at rest.
    # Disabled by default (local dev has no tenant). The generic LLM credential is the
    # documented exception and does NOT use this path. `entra_token_refresh_leeway_seconds`
    # refreshes a cached token before it actually expires; `entra_http_timeout_seconds` bounds
    # the token/OBO HTTP calls.
    entra_workload_federation_enabled: bool = False
    entra_workload_client_id: str = ""
    entra_token_endpoint: str = ""
    entra_sa_token_path: str = "/var/run/secrets/azure/tokens/azure-identity-token"
    entra_token_refresh_leeway_seconds: float = Field(default=300.0, gt=0)
    entra_http_timeout_seconds: float = Field(default=10.0, gt=0)
    # How long the front door waits between JWKS re-fetches forced by a token whose `kid` is not
    # in the cached key set. PyJWT re-fetches on *every* such miss, and the `kid` is chosen by an
    # unauthenticated caller, so without a floor one credential-less request becomes one outbound
    # request to the tenant IdP — an amplifier that also queues on the validation thread pool.
    # The cost is rotation latency: a genuinely new signing key is picked up after at most this
    # long instead of on the first token that uses it. That is bounded and configurable, and the
    # 300 s `lifespan` of the key cache already admits staleness of its own.
    entra_jwks_refresh_cooldown_seconds: float = Field(default=60.0, ge=0)
    # On-Behalf-Of exchange (plan F4-T4): when a backend acts for a specific user against a
    # user-scoped resource (ELN/LIMS), it swaps the user's token OBO for a downstream token so
    # the resource sees the real user, not the service. Generic and dormant — off until a
    # user-scoped source (the deferred custom Snowflake ELN connector) opts in by calling
    # `exchange_obo`.
    entra_obo_enabled: bool = False

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
        """Under `entra_required`, fail fast on a half-configured identity setup (review finding).

        Two footguns the front-door/authorization code cannot catch at request time:
        - an empty `entra_audience` (or no tenant/issuer/JWKS) makes every token rejected — a
          deny-all availability outage that should surface at startup, not as mysterious 401s.
          The issuer and the JWKS endpoint derive independently from the tenant, so each needs
          its own source: an issuer alone cannot resolve the keys endpoint;
        - naming an expensive action with **no** privileged role closes that action to everyone:
          `authz.authorize_trigger` fails closed on an empty role set, so the very action the
          operator singled out is refused for every user, with no role in existence that could
          pass it. That is a silent deny-all on one deliberate path, and it stays an error.

        **The converse is a valid configuration, and rejecting it was a shipped contradiction.**
        This validator used to demand the two settings be set *together*, on the reasoning that a
        role without an action gated nothing. That stopped being true when `expensive: true` in a
        `connector.yaml` started deriving into the gate (`authz.expensive_actions`): the action set
        now comes from the manifests, so `entra_privileged_roles` alone is the *complete* and
        intended configuration — it is the operator's remedy for a deployment whose declared
        expensive jobs currently refuse everyone, and it is exactly what `docs/guides/runbook.md`
        instructs. Requiring `entra_expensive_actions` beside it would force operators to
        hand-maintain the list of job names the derivation exists to eliminate, and a hand-copied
        list of other people's job names goes stale the first time a bundle adds one.

        Hence the asymmetry: actions without roles is a deny-all mistake, roles without actions is
        the normal production setup.
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
