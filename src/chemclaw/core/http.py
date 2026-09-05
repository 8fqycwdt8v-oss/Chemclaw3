"""The HTTP fact more than one layer needs: what an address means.

One primitive rather than a module of its own, because it answers "how do we talk about somebody
else's HTTP endpoint" and exists only to stop a second copy appearing:

- **`default_ssl_context`** — one TLS trust store for the whole process, because building one is
  expensive and every caller wants the same one. Measured below.
- **`is_loopback_host` / `is_loopback_url`** — the one definition of "this address cannot be
  reached from the network", which every safety rule in the tree that asks the question calls: the
  front door refuses to boot unauthenticated on a non-loopback *bind* (`api.middleware`, SEC-2), it
  refuses to boot pointed at the dev model gateway on a non-loopback bind (`api.middleware`), a
  connector manifest refuses `auth: mode: none` for a non-loopback *endpoint*
  (`connectors.manifest`), and the egress guard permits a *destination* without allowlisting it
  (`core.netguard`). The questions differ; the answer must not, or one of them would be enforcing a
  weaker notion of "safe address" than the other claims. It lives here because `connectors -> api`
  is an edge the layering policy explicitly removed (`tests/test_layering.py`) and because
  `core.netguard` arms at config import, so the definition has to sit below both.

  **It was a set of three literal strings and a parsed predicate, and they disagreed.** Measured on
  2026-09-05, before this was one function: a second address in `127.0.0.0/8`, and the unspecified
  address, were loopback to the guard and not to the front door — so a pod bound non-loopback with
  its gateway on such an address passed `_refuse_unconfigured_llm_gateway`, the check written to
  stop exactly that, and then failed every turn on a refused connection. (The addresses are
  described rather than written as URLs: `tests/test_no_egress.py` scans this file's *text* for
  `http(s)://` host literals and cannot tell a measurement in a docstring from a default in code,
  which is the guard working. `tests/test_netguard.py` holds them as data.)

  The set could not simply be widened to close it, because the two roles genuinely differ on one
  address: `0.0.0.0` as a *bind* is every interface (the whole subject of SEC-2) and as a
  *destination* never leaves the host. So this answers the narrow question only — the unspecified
  address is **not** loopback here — and `core.netguard` no longer exempts it. Nothing dialled it:
  the tree holds no `0.0.0.0` URL, and no local-server bind idiom reaches the guard with an
  unspecified or empty host (asyncio and `socket.create_server` resolve an IP literal without ever
  calling the resolver).

  **There is a third loopback constant and it is deliberately not this one.**
  `core.config.PG_LOOPBACK_HOSTS` asks a different question — *is this connection local*, for a TLS
  or plaintext exemption — and its extra member `""` is what makes the difference load-bearing
  rather than stylistic: a sink or channel URL with no host at all (a `file://` outbox) reads as
  local there, and `cli/validate_channels.py` already records the sharp edge in the present tense,
  that any "could not tell" answer therefore takes the exemption. Measured on 2026-09-05 across its
  three readers (`publish/drivers/postgres`, `publish/drivers/http`, `deliver/driver`), swapping it
  for `is_loopback_host` moves behaviour in **both** directions: it *widens* the exemption for
  `127.0.0.2`, the rest of `127.0.0.0/8`, a bracketed `[::1]` and a zone id, and it *narrows* it for
  the empty host, which would start demanding TLS of a hostless sink. Neither is a rename, both are
  publish-path behaviour changes with their own refusal messages, so the two stay separate and this
  paragraph is the one place that says why. `tests/test_netguard.py` names it as the single allowed
  second constant, so a *fourth* still fails.

  An address the parser cannot read falls on the demanding side, which is conservative in every
  caller: a bind refuses to boot, an endpoint demands a credential, a destination demands an
  allowlist entry. None of them waives anything. An IPv4-mapped literal follows its mapped address
  in both directions (`::ffff:127.0.0.1` is loopback, `::ffff:8.8.8.8` is not) — `ipaddress` does
  that itself, and this docstring said the opposite until `tests/test_netguard.py` was run.

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

import ipaddress
import ssl
from functools import cache
from typing import Any
from urllib.parse import urlsplit


def is_loopback_host(host: str | None) -> bool:
    """Whether `host` is unreachable from the network — decided by parsing, never by name.

    `localhost` and any literal that parses as a loopback IP (the whole of `127.0.0.0/8`, `::1`)
    qualify. A *name* is never resolved — in `core.netguard` resolving it would itself be egress —
    so a `.localhost` suffix is not trusted: an `/etc/hosts` line or a wildcard zone would otherwise
    turn the suffix into "any destination" (the sibling fleet guard's own recorded bug). A bracketed
    IPv6 literal and a zone id (`[::1]`, `fe80::1%eth0`) are read; the unspecified address
    (`0.0.0.0`, `::`) and an empty host are not loopback, because as a *bind* they mean every
    interface.

    Args:
        host: A bare host — a settings field like `service_host`, or a socket address's first
            element. `None` and `""` answer False.

    Returns:
        True when the address cannot be reached from the network.
    """
    if not host:
        return False
    bare = host.strip("[]").lower()
    if bare == "localhost":
        return True
    try:
        return ipaddress.ip_address(bare.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    """Whether `url`'s host is a loopback interface — i.e. unreachable from the network.

    Conservative by construction: a URL whose host cannot be parsed at all (`urlsplit` raising on a
    malformed IPv6 literal, or a bare path with no authority) is *not* loopback. Every caller uses
    this to decide whether a credential is required, so the unparseable case must fall on the side
    that demands one rather than the side that waives it.

    Args:
        url: An absolute URL, e.g. a connector endpoint's `http://127.0.0.1:8811/mcp`.

    Returns:
        `is_loopback_host` of the URL's host.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return is_loopback_host(host)


@cache
def default_ssl_context() -> ssl.SSLContext:
    """The process's one TLS trust store, built once.

    **Why this exists at all: constructing it is not cheap, and httpx does it per client.** An
    `httpx.AsyncClient` with no `verify=` builds a fresh `ssl.SSLContext` and parses the whole
    certifi CA bundle into it. A turn opens one client per connector, so the shipped seven-connector
    deployment paid that seven times *per turn*, on the single event loop that serves every user on
    the pod, before any tool ran. Measured in this project's environment:

        7 default clients (one turn): 156.1 ms
        7 shared-context clients    :   0.4 ms

    — a 390x difference, and `load_verify_locations` was the largest single entry in a cProfile of
    connector setup (0.433 s of 1.371 s). It is *blocking* CPU rather than await time, so it does
    not merely slow the turn that pays it: at the shipped eight-turn admission cap it stalled every
    other user's stream on the pod for over a second, and there is a cliff behind that — around
    45-50 concurrent opens the client's own CPU exceeds `connector_open_timeout_seconds`, healthy
    connectors are recorded unreachable, and the pod then serves turns with no tools at all.

    **Sharing one context between clients is safe and is what httpx documents.** A context is
    read-only in use, carries no per-connection state, and holds the same trust decision every
    caller here wants: the system trust store as certifi sees it. A caller needing a *different*
    trust decision — a private CA — passes its own `verify=` and does not come here; the embedding
    client already does exactly that.

    Most endpoints this is handed to are plain in-cluster `http://`, where the context is never
    consulted at all. That is not a reason to skip it: the cost was paid on construction, not on
    use, which is precisely why it was invisible.
    """
    return ssl.create_default_context()


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
    return {"verify": ssl.create_default_context(cafile=ca_bundle), "trust_env": False}
