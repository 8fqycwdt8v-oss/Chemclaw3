"""Front-door user authentication via Azure Entra ID.

Validates the Entra OIDC token on every non-health request and turns it into a `Principal` (object
id, name, app roles) that authorizes and attributes every backend action. Checks the signature
against the tenant JWKS, the issuer, and the audience — the confused-deputy guard, since a token
minted for another resource must be rejected. With `entra_required` False (local dev only) a fixed
stand-in principal is used. `_signing_key` is the one indirection tests replace.
"""

import asyncio
import logging
import ssl
import threading
import time
from functools import cache
from typing import Annotated, Any

import httpx
import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError
from pydantic import BaseModel, StringConstraints

from chemclaw.api.middleware import (
    AT_CAPACITY,
    arrived_over_the_network,
    bind_request_actor,
    note_network_exposure,
    route_template,
)
from chemclaw.api.rate_limit import RateLimited, enforce_request_budget
from chemclaw.api.state import state
from chemclaw.core.config import settings
from chemclaw.core.http import default_ssl_context
from chemclaw.core.identity_context import GROUP_ROLE_PREFIX
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# Re-exported: the prefix belongs to `core.identity_context`'s role vocabulary and is still read
# as `auth.GROUP_ROLE_PREFIX`.
__all__ = [
    "GROUP_ROLE_PREFIX",
    "AuthError",
    "Principal",
    "reauthorize",
    "refuse_unusable_entra_ca_bundle",
    "require_principal",
    "validate_token",
]

# The dev stand-in used only when `entra_required` is False (local, no tenant). Never reached in a
# real deployment, where every request is a validated Entra token.
DEV_PRINCIPAL_OID = "dev-user"
# Public spelling so a per-actor guard can recognise the one principal that is not an actor (see
# `chemclaw.api.routes.turns`).
_DEV_PRINCIPAL_OID = DEV_PRINCIPAL_OID


class Principal(BaseModel):
    """An authenticated Entra user: the identity every backend action is attributed to.

    **`oid` is stripped at construction**, because the turn reads the actor stripped
    (`core/identity_context.get_current_actor`) and the skills and proposals routes key on this
    field raw: a whitespace-bearing oid (a dev or configured principal) saved a skill under
    `' alice '` that turns mounting `'alice'` never read. Normalising at the source is what makes
    the two spellings one; a blank one is refused rather than stripped to an empty identity.
    """

    oid: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    upn: str = ""
    roles: frozenset[str] = frozenset()


class AuthError(Exception):
    """A token could not be validated (bad signature, audience, issuer, or missing identity)."""


class IdentityProviderUnavailable(Exception):
    """The tenant JWKS could not be reached, so no token can be validated right now.

    Not an `AuthError`: an unreachable IdP is our outage (503), not the caller's bad credential
    (401).
    """


class _HttpxJwkClient(PyJWKClient):
    """PyJWT's JWKS client with its one network call moved onto `httpx`.

    Upstream fetches via `urlopen`, which follows the process's `HTTPS_PROXY`, so a proxy could
    answer with a key set of its choosing. The fetch here uses `trust_env=False`, a per-request
    decision with no process-global state. `verify=` is `_tenant_ssl_context`, built once.
    Overriding `fetch_data` relies on an undocumented upstream method, pinned in
    `tests/test_upstream_surface.py`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Upstream's client, plus the lock and the memory that make one fetch serve a crowd.

        Fetches per client are serialised (upstream's expiry read and our forced refresh can both
        call `fetch_data`), and a caller that waited is answered by the fetch it waited behind. A
        failure is remembered for `entra_jwks_failure_backoff_seconds`, so an IdP fault is not one
        fetch per request.
        """
        super().__init__(*args, **kwargs)
        self._fetch_lock = threading.Lock()
        # When the last fetch finished, and what it yielded: the key set, or the 503 it raised.
        self._fetched_at: float | None = None
        self._outcome: Any = None

    def fetch_data(self) -> Any:
        """One fetch at a time per client; a caller that waited for one is answered by it.

        A failure is answered from memory for `entra_jwks_failure_backoff_seconds`, as the same
        `IdentityProviderUnavailable`. `_fetch` is the fetch itself.
        """
        asked = time.monotonic()
        with self._fetch_lock:
            outcome, fetched_at = self._outcome, self._fetched_at
            if fetched_at is not None:
                # A fetch finished while this caller queued: its answer is this caller's too.
                waited_behind_one = fetched_at >= asked
                failing = isinstance(outcome, IdentityProviderUnavailable)
                backing_off = failing and (
                    time.monotonic() - fetched_at < settings.entra_jwks_failure_backoff_seconds
                )
                if waited_behind_one or backing_off:
                    if failing:
                        # A fresh instance: re-raising the stored one would grow its traceback
                        # by a frame on every request it answered.
                        raise IdentityProviderUnavailable(str(outcome)) from outcome
                    return outcome
            try:
                keys = self._fetch()
            except IdentityProviderUnavailable as unavailable:
                # Remember a copy without a traceback: the raised one holds frames with the raw
                # bearer token.
                self._outcome = IdentityProviderUnavailable(str(unavailable))
                self._fetched_at = time.monotonic()
                raise
            self._outcome, self._fetched_at = keys, time.monotonic()
            return keys

    def _fetch(self) -> Any:
        """The tenant's key set, fetched off the environment's proxy and mapped onto our split.

        Every fetch failure is `IdentityProviderUnavailable` (503), never 401:

        * `httpx.TransportError` — refused, unreachable, timed out, or a malformed endpoint.
        * `httpx.HTTPStatusError` — the tenant answered with an error (wrong tenant, IdP fault).
        * `ValueError` — a 200 whose body is not JSON, e.g. an intercepting proxy's error page.

        A redirect is refused explicitly, before `raise_for_status`: following it would move the key
        set's origin past `core/netguard.py`'s allowlist. Our own exception type is raised because
        PyJWT catches nothing around `fetch_data`. The cache write reproduces upstream's, or PyJWT's
        key-set cache would be disabled; pinned in `tests/test_upstream_surface.py`.
        """
        try:
            response = httpx.get(
                self.uri,
                headers=self.headers,
                timeout=self.timeout,
                # Never inherit an ambient proxy — the whole reason this class exists.
                trust_env=False,
                verify=_tenant_ssl_context(settings.entra_ca_bundle),
            )
            if response.is_redirect:
                raise IdentityProviderUnavailable(
                    f"tenant JWKS endpoint redirected ({response.status_code}); the key set must "
                    "come from the declared address"
                )
            response.raise_for_status()
            jwk_set = response.json()
        except httpx.HTTPStatusError as exc:
            raise IdentityProviderUnavailable(
                f"tenant JWKS endpoint answered {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise IdentityProviderUnavailable(f"tenant JWKS unreachable: {exc}") from exc
        except ValueError as exc:
            raise IdentityProviderUnavailable(f"tenant JWKS unusable: {exc}") from exc
        if self.jwk_set_cache is not None:
            try:
                self.jwk_set_cache.put(jwk_set)
            except (jwt.PyJWTError, ValueError) as exc:
                # PyJWT parses inside `put` and raises before storing; treat it as a fetch failure
                # so `fetch_data` remembers it.
                raise IdentityProviderUnavailable(f"tenant JWKS unusable: {exc}") from exc
        return jwk_set


@cache
def _tenant_ssl_context(ca_bundle: str) -> ssl.SSLContext:
    """The trust store the tenant's key set is fetched under: `ca_bundle`, else certifi.

    Unset, the process's shared certifi context. Set, the bundle replaces certifi rather than
    joining it, so the mounted file is the complete answer. `SSL_CERT_FILE`/`SSL_CERT_DIR` are
    ignored, and there is no way to turn verification off: the key set is what every token is
    validated against. Cached per path; a rotated bundle is picked up on restart.

    Raises:
        OSError: the path does not name a readable file.
        ssl.SSLError: the file holds no PEM certificate.
    """
    if not ca_bundle:
        return default_ssl_context()
    return ssl.create_default_context(cafile=ca_bundle)


def refuse_unusable_entra_ca_bundle() -> None:
    """Refuse to boot when `entra_ca_bundle` names a file that is not a usable CA bundle.

    At boot rather than at the first fetch, where it would surface as a 500 on every request. Uses
    the same cached `_tenant_ssl_context` the fetch uses, and runs whether or not `entra_required`
    is on.

    Raises:
        RuntimeError: the path is missing, unreadable, a directory, or holds no PEM certificate.
    """
    ca_bundle = settings.entra_ca_bundle
    if not ca_bundle:
        return
    try:
        _tenant_ssl_context(ca_bundle)
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError(
            f"CHEMCLAW_ENTRA_CA_BUNDLE={ca_bundle!r} is not a usable CA bundle ({exc}). It must "
            "name a readable file holding at least one PEM-encoded CA certificate; the tenant's "
            "JWKS endpoint is verified against it instead of certifi. Unset it to verify against "
            "certifi — verification itself cannot be turned off."
        ) from exc


# One JWKS client per endpoint, cached: `PyJWKClient` keeps its own key cache, so rebuilding it per
# request would re-fetch the JWKS on the hot path. Keyed by endpoint so a config change applies.
_jwks_clients: dict[str, PyJWKClient] = {}

# When an unknown `kid` last forced a JWKS re-fetch, per endpoint. The `kid` comes from an
# unauthenticated token header and PyJWT re-fetches on any unknown one, so without this gate each
# credential-less request would cost an outbound fetch.
_forced_refresh_lock = threading.Lock()
_last_forced_refresh: dict[str, float] = {}


def _client_for(endpoint: str) -> PyJWKClient:
    """The cached JWKS client for `endpoint`, built on first use with our configured timeout.

    No lock: `setdefault` is atomic, so a race builds a second client and discards it, and every
    caller gets the stored one. The constructor does no I/O, so a discarded client costs nothing.
    """
    client = _jwks_clients.get(endpoint)
    if client is None:
        client = _jwks_clients.setdefault(
            endpoint,
            # `cooldown_duration=0`: the refresh cooldown is ours (`_forced_refresh_allowed`);
            # PyJWT's own would compose with it and stretch `entra_jwks_refresh_cooldown_seconds`.
            _HttpxJwkClient(
                endpoint, timeout=settings.entra_http_timeout_seconds, cooldown_duration=0
            ),
        )
    return client


def _match_kid(signing_keys: list[Any], kid: str) -> Any | None:
    """The key in `signing_keys` whose id is `kid`, or `None`.

    Local rather than `PyJWKClient.match_kid`, to avoid a second undocumented upstream dependency.
    """
    return next((key for key in signing_keys if key.key_id == kid), None)


def _forced_refresh_allowed(endpoint: str, now: float) -> bool:
    """Whether an unknown `kid` may pay for a JWKS re-fetch — at most once per cooldown.

    Records the attempt when it grants one, so the first caller after a key rotation pays and later
    callers read the refreshed cache.
    """
    # Locked: concurrent check-then-set would grant two refreshes, and upstream's lock would only
    # serialise them.
    with _forced_refresh_lock:
        last = _last_forced_refresh.get(endpoint)
        if last is not None and now - last < settings.entra_jwks_refresh_cooldown_seconds:
            return False
        _last_forced_refresh[endpoint] = now
    return True


def _signing_key(token: str) -> Any:
    """Resolve the RSA signing key for `token` from the tenant JWKS (indirected for tests).

    The fetch is blocking I/O, so callers on the event loop run validation in a worker thread; it is
    bounded by `entra_http_timeout_seconds`. A cached `kid` costs no network. An unknown one may
    force a re-fetch at most once per `entra_jwks_refresh_cooldown_seconds`, and is otherwise an
    `AuthError`: the caller must not choose how much work we do.
    """
    endpoint = settings.entra_jwks_endpoint
    client = _client_for(endpoint)
    # Raises `DecodeError` (an `InvalidTokenError`) on a malformed token, which `validate_token`
    # already turns into a 401 — so garbage never reaches the network at all.
    kid = jwt.get_unverified_header(token).get("kid")
    if not kid:
        raise AuthError("token header carries no 'kid'")
    try:
        cached = _match_kid(client.get_signing_keys(), kid)
        if cached is not None:
            return cached.key
        if not _forced_refresh_allowed(endpoint, time.monotonic()):
            raise AuthError(f"no signing key matches kid {kid!r} (refresh on cooldown)")
        return client.get_signing_key(kid).key
    except PyJWKClientError as exc:
        # A `PyJWKClientError` here is an unknown key: the caller's problem. An unreachable tenant
        # is `IdentityProviderUnavailable`, raised at the fetch, and passes through this frame
        # untouched.
        raise AuthError(f"no signing key matches kid {kid!r}: {exc}") from exc
    except (ValueError, jwt.PyJWTError) as exc:
        # The IdP answered with something that is not a usable key set (`PyJWKSetError` and other
        # `PyJWTError`s, or a `ValueError`). That is our outage, not a bad credential, so 503 rather
        # than 401. Last, so the arms above keep their meaning; an `AuthError` raised in the `try`
        # passes through.
        raise IdentityProviderUnavailable(f"tenant JWKS unusable: {exc}") from exc


def validate_token(token: str) -> Principal:
    """Validate an Entra OIDC token and return its `Principal`, or raise `AuthError`.

    Verifies the RS256 signature against the tenant JWKS, the audience (`entra_audience`, the
    confused-deputy guard) and the issuer, then extracts the identity claims.
    """
    try:
        key = _signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.entra_audience,
            issuer=settings.entra_issuer_url,
            # Require an expiry: PyJWT only checks `exp` when present, so reject a token that omits
            # it (Entra always issues one; this closes the no-exp edge). (review finding)
            options={"require": ["exp"]},
        )
    except jwt.InvalidTokenError as exc:  # signature/audience/issuer/expiry all funnel here
        raise AuthError(f"invalid token: {exc}") from exc
    return _principal_from_claims(claims)


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Build a `Principal` from validated claims (`oid` is mandatory — no anonymous identity).

    Under `entra_group_claims_as_roles` the token's `groups` join the same entitlement set every
    gate reads, so no gate has to decide separately whether to consult groups.
    """
    oid = claims.get("oid")
    # Checked as `Principal` will check it (a string, non-empty once stripped), so a malformed
    # claim is a 401 here rather than a pydantic `ValidationError` escaping as a 500.
    if not isinstance(oid, str) or not oid.strip():
        raise AuthError("token has no 'oid' claim")
    upn = claims.get("preferred_username") or claims.get("upn") or ""
    if not isinstance(upn, str):
        raise AuthError("token's 'preferred_username'/'upn' claim is not a string")
    entitlements = _string_list_claim(claims, "roles")
    if settings.entra_group_claims_as_roles:
        # Entra emits `_claim_names` instead of `groups` when a user is in too many groups. That is
        # an overage, not an empty membership; resolving it needs a Graph call, which is not
        # permitted, so it is logged rather than silently treated as no groups.
        if "groups" not in claims and "_claim_names" in claims:
            # Counted as well as logged: the counter is what makes someone look, and the chemist
            # sees only a gated share returning nothing.
            record_metric(lambda m: m.increment("chemclaw_group_claim_overage_total"))
            logger.warning(
                "token for %s carries a group-claim overage rather than 'groups'; "
                "group-derived entitlements are unavailable for this user",
                oid,
            )
        # Namespaced: group claims may be names rather than object ids (a tenant setting), so an
        # unprefixed group could match a privileged app role and widen the write-tool gates.
        entitlements += [
            f"{GROUP_ROLE_PREFIX}{group}" for group in _string_list_claim(claims, "groups")
        ]
    return Principal(oid=oid, upn=upn, roles=frozenset(entitlements))


def _string_list_claim(claims: dict[str, Any], name: str) -> list[str]:
    """The claim `name` as a list of strings — absent is empty, any other shape is an `AuthError`.

    Checked rather than coerced: `list("ab")` would grant roles `a` and `b`, and other shapes would
    surface as a 500 instead of a 401.
    """
    if name not in claims:
        return []
    value = claims[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuthError(f"token's {name!r} claim is not a list of strings")
    return value


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: the validated Entra user for this request (401 if required and absent).

    With `entra_required` False (local dev) a fixed dev principal is returned; otherwise a
    missing/invalid `Authorization: Bearer` token is a 401. Validation runs in a worker thread
    because a JWKS cache miss is a blocking fetch, and every stream shares the event loop.
    """
    _shed_if_the_database_is_known_down(request)
    if not settings.entra_required:
        _refuse_exposed_dev_principal(request)
        return _bind(
            request, _within_budget(Principal(oid=_DEV_PRINCIPAL_OID, upn="dev@localhost"))
        )
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        # Counted and logged so a client sending no header is distinguishable from no failures.
        # `info` because unauthenticated probes are ordinary internet traffic; the counter makes a
        # rate alertable.
        _count_auth_failure("missing")
        # Log the route template, never `request.url.path`: on this unauthenticated path the path is
        # caller-authored, unbounded text that would stall the redacting log filter and inject into
        # logs. An unrouted request reads `<unmatched>`.
        logger.info("request to %s carried no bearer token", route_template(request.scope))
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        principal = await asyncio.to_thread(validate_token, header[len("Bearer ") :])
    except IdentityProviderUnavailable as exc:
        # 503, not 401: we could not reach the tenant to decide. `warning` rather than `info`
        # because this one is actionable — the token is fine and the dependency is not.
        _count_auth_failure("provider_unavailable")
        logger.warning("identity provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="identity provider unavailable") from exc
    except AuthError as exc:
        # The specific failure reason (audience/issuer/expiry mismatch) is logged for the operator,
        # never disclosed to the caller, who gets a generic 401.
        _count_auth_failure("invalid")
        logger.info("token validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc
    return _bind(request, _within_budget(principal))


async def reauthorize(request: Request, principal: Principal) -> Principal:
    """What this request's credential establishes now — `AuthError` once it no longer does.

    For work that starts long after its request was authenticated: a message that waited in a shared
    session's queue runs its turn up to `service_turn_queue_max` × `service_turn_timeout_seconds`
    later. Re-runs the same validation (signature, audience, issuer, `exp`); roles come only from
    the token, so expiry is how they change. A token now naming someone else is refused. Not
    `require_principal`, which would also charge the rate budget and rebind ambients.

    Under `entra_required=False` there is no credential to re-check and the dev principal stands.

    Raises:
        AuthError: the credential no longer validates, or names a different principal.
        IdentityProviderUnavailable: the tenant's key set could not be reached to decide.
    """
    if not settings.entra_required:
        return principal
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthError("the request carries no bearer token to re-check")
    fresh = await asyncio.to_thread(validate_token, header[len("Bearer ") :])
    if fresh.oid != principal.oid:
        raise AuthError("the request's token now names a different principal")
    return fresh


def _shed_if_the_database_is_known_down(request: Request) -> None:
    """Answer at once when the readiness probe has just found Postgres unreachable.

    Otherwise every request waits out `pg_pool_timeout_seconds` holding a connection and concurrency
    slot. Only a verdict younger than `service_readiness_cache_seconds` sheds, since only `/readyz`
    re-probes. Living under `require_principal` exempts the probes, which matters: `/readyz` is the
    only thing that can clear the verdict. Routes that need no database are shed too; the pod is
    already out of rotation. The message matches `_database_unavailable`'s.

    Raises:
        HTTPException: 503, when a current readiness probe says the database is unreachable.
    """
    front = state(request)
    window = settings.service_readiness_cache_seconds
    if front.database_reachable or time.monotonic() - front.database_probed_at > window:
        return
    record_metric(lambda m: m.increment("chemclaw_db_unavailable_total"))
    raise HTTPException(status_code=503, detail=AT_CAPACITY)


def _refuse_exposed_dev_principal(request: Request) -> None:
    """Refuse to mint the dev principal for a request that arrived from the network.

    The boot guard reads `settings.service_host`, which uvicorn's `--host` can contradict; this
    reads the socket the request actually arrived on. Dev branch only — with `entra_required` an
    off-box request is an ordinary 401. `service_allow_insecure=true` opts out, as at boot. 503
    because no credential would help: the service is misconfigured.

    Raises:
        HTTPException: 503, when an unauthenticated deployment is serving the network.
    """
    if settings.service_allow_insecure or not arrived_over_the_network(request.scope):
        return
    server = request.scope.get("server") or ("", None)
    _count_auth_failure("network_exposed")
    note_network_exposure(str(server[0]))
    raise HTTPException(status_code=503, detail="service misconfigured; refusing to serve")


def _count_auth_failure(reason: str) -> None:
    """Book one refused authentication under its reason — a closed, four-value label set.

    Via `record_metric`, since this module is imported by processes that do not own the registry.
    """
    record_metric(lambda m: m.increment("chemclaw_auth_failures_total", labels={"reason": reason}))


def _bind(request: Request, principal: Principal) -> Principal:
    """Make the authenticated caller ambient for the rest of the request, then return it.

    Here because every authenticated route funnels through this function, so every log line names
    its actor. The reset is `api/middleware._RequestObservability`'s, which runs on every exit path.
    """
    bind_request_actor(request, principal.oid, principal.roles)
    return principal


def _within_budget(principal: Principal) -> Principal:
    """Spend one request against this principal's rate budget, or 429.

    Here because every authenticated route funnels through `require_principal`, so a new route
    cannot skip it; the policy is `api/rate_limit.py`'s. After validation, so the limit is per
    person, not per credential. Probe routes do not depend on this and are never limited.
    """
    try:
        enforce_request_budget(principal.oid)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail="too many requests",
            # Seconds until one token refills, so a client backs off by the right amount rather
            # than guessing — the same courtesy the budget guard's 429 already extends.
            headers={"Retry-After": str(max(1, int(exc.retry_after_seconds + 0.999)))},
        ) from exc
    return principal
