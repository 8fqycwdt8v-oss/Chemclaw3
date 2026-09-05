"""The HTTP fact more than one layer needs: what an address means.

One primitive rather than a module of its own, because it answers "how do we talk about somebody
else's HTTP endpoint" and exists only to stop a second copy appearing:

- **`LOOPBACK_HOSTS` / `is_loopback_url`** — the one definition of "this address cannot be reached
  from the network", which two unrelated safety rules ask about: the front door refuses to boot
  unauthenticated on a non-loopback *bind* (`api.middleware`, SEC-2), and a connector manifest
  refuses `auth: mode: none` for a non-loopback *endpoint* (`connectors.manifest`). The two
  questions differ; the answer must not, or one of them would be enforcing a weaker notion of
  "safe address" than the other claims. It lives here because `connectors -> api` is an edge the
  layering policy explicitly removed (`tests/test_layering.py`).

- **`private_ca_transport`** — the two decisions an internal, privately-signed endpoint forces on
  an httpx client, stated once. Both LLM seams need them and neither may own them: the chat client
  (`agent/llm_provider._tls_http_client`, async) and the embedding client
  (`core/embeddings._openai_client`, sync) reach the *same* gateway with the same CA bundle, and
  they wrote the same two lines separately. A `dict` of kwargs rather than a client, because the
  only thing that legitimately differs between them is the class — and returning a client would
  force this module to pick sync or async for a caller that already knows.

There was a second primitive, `error_detail`, and the paragraph above used to say in the present
tense that "several modules (the Nextflow launcher, the Entra token/OBO exchanges)" called it. All
three were deleted — the launcher with the HPC tier in
`D-2026-08-26-semiempirical-is-the-whole-tier`, the OBO exchange in
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` — and the function outlived its
callers while the prose outlived the function. Whoever needs a bounded quotation of somebody else's
error body again should write it back with the caller that needs it, not before.
"""

from typing import Any
from urllib.parse import urlsplit

# Loopback interfaces: an address here is reachable only from the local host, so an unauthenticated
# service or connector on one is not a network-exposed footgun. Anything else is — notably the
# `service_host="0.0.0.0"` default and every in-cluster Service name.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    """Whether `url`'s host is a loopback interface — i.e. unreachable from the network.

    Conservative by construction: a URL whose host cannot be parsed at all (`urlsplit` raising on a
    malformed IPv6 literal, or a bare path with no authority) is *not* loopback. Every caller uses
    this to decide whether a credential is required, so the unparseable case must fall on the side
    that demands one rather than the side that waives it.

    Args:
        url: An absolute URL, e.g. a connector endpoint's `http://127.0.0.1:8811/mcp`.

    Returns:
        True when the host is one of `LOOPBACK_HOSTS`.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def private_ca_transport(ca_bundle: str) -> dict[str, Any] | None:
    """The httpx client kwargs for an endpoint behind a private CA, or None for the system store.

    Two decisions, both of which were made twice before this existed:

    - **An `SSLContext`, not `verify="<path>"`.** httpx deprecated the string form
      ("`verify=<str>` is deprecated. Use `verify=ssl.create_default_context(cafile=...)`"), and
      building the context is also the only form that says what the bundle *is* — a CA file to
      verify the peer against, rather than a path httpx has to guess the meaning of.
    - **`trust_env=False`.** `HTTPS_PROXY`/`ALL_PROXY` set on the pod would otherwise redirect
      every prompt, completion, embedded note and `Authorization` bearer to a host of the env
      setter's choosing, *past* the CA pinning above — a proxy re-terminates TLS.

    Returns None when no bundle is configured, which leaves the SDK's own default client in place:
    the right behaviour for a publicly-trusted endpoint, and the reason this returns kwargs rather
    than raising.

    Args:
        ca_bundle: Path to the CA bundle (`settings.llm_tls_ca_bundle`), or "" for none.

    Returns:
        Kwargs for `httpx.Client(**kwargs)` / `httpx.AsyncClient(**kwargs)`, or None.
    """
    if not ca_bundle:
        return None
    import ssl

    return {"verify": ssl.create_default_context(cafile=ca_bundle), "trust_env": False}
