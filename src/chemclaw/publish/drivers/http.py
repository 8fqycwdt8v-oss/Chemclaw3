"""Publishing to a service that accepts a JSON document.

The second shape a site's results store takes: not a database a DBA runs our DDL on, but a REST
endpoint — a LIMS, an internal data platform, a queue in front of one. What it receives is the
canonical record as JSON, the same content the SQL driver spreads across tables, so a site can move
between the two without the records changing meaning.

**The URL is configuration, never a default in source.** `tests/test_no_egress.py` bans a
third-party data *host* baked into a shipped module, and permits an address a deployment configures
— the same class the LLM endpoint and Temporal are in. There is no default here
at all: a sink with no URL is a manifest error, not a silent fallback to somebody's service.

**The credential is an environment variable name, read per request.** Reading at request time is
what makes a rotated secret take effect without a restart; the same discipline
`connectors/identity.py` applies to a connector's bearer token, and the name is registered with the
log-redaction inventory so a driver echoing its own configuration cannot leak it.
"""

import logging
import os
import time
from collections.abc import Sequence
from typing import Any

import httpx

from chemclaw.core.config import settings
from chemclaw.core.logging import log_event, register_secret_env
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.publish.driver import SinkRejectedError, SinkUnavailableError
from chemclaw.publish.record import ResultRecord

logger = logging.getLogger(__name__)

# Statuses worth trying again: the far end is overloaded, restarting, or behind a proxy that is.
# Everything else in 4xx is a statement about the content, which a retry cannot change.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpResultSink:
    """POSTs published records as a JSON document to a configured endpoint."""

    def __init__(
        self,
        *,
        name: str,
        tenant_id: str,
        url: str,
        token_env: str = "",
        timeout_seconds: float = 30.0,
        writer_version: str = "",
        verify_tls: bool = True,
    ) -> None:
        """Hold the endpoint and the *name* of the variable its credential lives in.

        Args:
            name: The sink's manifest name, for log lines and errors.
            tenant_id: What this deployment calls itself on every published record.
            url: Where to POST. Required — there is deliberately no default.
            token_env: Environment variable holding a bearer token, read per request. Empty means
                the endpoint takes no credential, which is only sensible on a loopback or a
                mesh-authenticated address.
            timeout_seconds: Per-request ceiling.
            writer_version: The ChemClaw release stamped on each published record. Defaults to
                this deployment's own revision; a manifest sets it only to override that.
            verify_tls: Left settable only so a site with an internal CA can point at its own
                bundle by other means; **never set this false** — an unverified TLS connection to a
                results store is an unauthenticated one.
        """
        if not url:
            raise ValueError(
                f"result sink {name!r} declares no url; an HTTP sink must name where it publishes"
            )
        self._name = name
        self._tenant_id = tenant_id
        self._url = url
        self._token_env = token_env
        self._timeout = timeout_seconds
        # Defaulted like the SQL sink's, and for the same reason: nothing computed a writer
        # version, so this crossed to every endpoint as `''` — a provenance field that reads as
        # "recorded, and blank". `deployment_revision` is what the audit trail already stamps.
        self._writer_version = writer_version or settings.deployment_revision
        self._verify = verify_tls
        if token_env:
            # Registered before it is ever read, so a traceback that echoes configuration cannot
            # put the token into a log line.
            register_secret_env(token_env)

    def _headers(self) -> dict[str, str]:
        """The request headers, with the credential read now rather than at construction."""
        headers = {"content-type": "application/json"}
        if self._token_env:
            token = os.environ.get(self._token_env)
            if not token:
                raise SinkRejectedError(
                    f"result sink {self._name!r} needs a bearer token in ${self._token_env}, "
                    "but that variable is unset or empty"
                )
            headers["authorization"] = f"Bearer {token}"
        return headers

    def _document(self, records: Sequence[ResultRecord]) -> dict[str, Any]:
        """The batch as one versioned document.

        `contract_version` rides on the envelope as well as on each record, so a receiver can route
        on it without unpacking — the same reason the SQL driver stamps it on every row.
        """
        return {
            "tenant_id": self._tenant_id,
            "writer_version": self._writer_version,
            "contract_version": records[0].contract_version,
            "records": [record.model_dump(mode="json") for record in records],
        }

    async def aclose(self) -> None:
        """Nothing to release: the client is scoped to a single delivery.

        A no-op with a reason rather than an omission. `deliver` opens its `AsyncClient` inside an
        `async with`, so the connection pool is already gone by the time the sink is discarded —
        which is why this sink never had the leak the SQL one did.
        """

    def _record(
        self,
        outcome: str,
        rows: int,
        seconds: float,
        *,
        status: int = 0,
        detail: str = "",
    ) -> None:
        """Time and name every delivery attempt — the thing this module declared a logger for.

        **The logger was declared and used zero times.** So a results endpoint that had been dead
        for a week produced no line and no number anywhere: every row simply spent its
        `result_publish_max_attempts` and was dead-lettered, and the only evidence was a counter
        that four unrelated failures share. There is no circuit breaker either, which makes the
        latency the operative signal — a sink timing out at 30 s per attempt is what turns a
        15-minute drain into one that never finishes its batch, and until this nothing measured it.

        `outcome` is bounded by construction: `timeout`, `unreachable`, or the response's status
        *class* — never the status itself, which would put an endpoint's error vocabulary into a
        log field's value space. The exact code rides as `status`.

        The histogram is labelled by sink and not by outcome: the question it answers is "how long
        does this destination take", and splitting the distribution by outcome would leave the
        timeouts — the samples that decide whether a drain finishes — in a series of their own.
        """
        record_metric(
            lambda m: m.observe("chemclaw_sink_delivery_seconds", seconds, {"sink": self._name})
        )
        log_event(
            logger,
            "sink.delivered" if outcome == "2xx" else "sink.failed",
            "result sink %r: %d row(s) -> %s in %.3fs%s",
            self._name,
            rows,
            outcome,
            seconds,
            f" ({detail[:200]})" if detail else "",
            level=logging.INFO if outcome == "2xx" else logging.WARNING,
            sink=self._name,
            outcome=outcome,
            status=status,
            rows=rows,
            duration_s=round(seconds, 3),
        )

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """POST the batch, classifying the response into retryable and not.

        The receiver is expected to be idempotent on `calc_ref` — the same promise the SQL driver
        keeps with content-addressed upserts. The outbox retries, so a receiver that appends
        instead of upserting will accumulate duplicates on any transient failure; that is stated
        here because it is the one thing this driver cannot enforce from its side.
        """
        if not records:
            return
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, verify=self._verify, trust_env=False
            ) as client:
                response = await client.post(
                    self._url, json=self._document(records), headers=self._headers()
                )
        except httpx.TimeoutException as exc:
            self._record("timeout", len(records), time.perf_counter() - started, detail=str(exc))
            raise SinkUnavailableError(
                f"result sink {self._name!r} timed out after {self._timeout}s: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            self._record(
                "unreachable", len(records), time.perf_counter() - started, detail=str(exc)
            )
            raise SinkUnavailableError(f"result sink {self._name!r} is unreachable: {exc}") from exc

        self._record(
            f"{response.status_code // 100}xx",
            len(records),
            time.perf_counter() - started,
            status=response.status_code,
        )
        if response.status_code in _RETRYABLE_STATUSES:
            raise SinkUnavailableError(
                f"result sink {self._name!r} answered {response.status_code}; will retry"
            )
        if response.status_code >= 400:
            # The body is included because it is the receiver's own account of what was wrong with
            # the content, and this failure is one an operator has to read to fix. Bounded, because
            # an HTML error page is not worth a log line of unbounded length.
            raise SinkRejectedError(
                f"result sink {self._name!r} refused the batch with {response.status_code}: "
                f"{response.text[:500]}"
            )
