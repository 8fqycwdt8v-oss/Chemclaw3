# D-2026-09-05-the-loopback-answer-is-parsed-and-there-is-one-of-it — a string set and a parser disagreed on four addresses

## Status

Accepted. Closes a hole opened by `D-2026-09-04-a-gateway-is-the-only-provider`, which is not
edited: the boot rule it added was correct and read a predicate that disagreed with the one the
egress guard uses.

## Context

Two functions answered "is this host loopback" and did not agree.

- `core/http.LOOPBACK_HOSTS` — a three-element set of literal strings.
- `core/netguard._is_loopback` — which parsed the address.

`D-2026-09-04-a-gateway-is-the-only-provider` added `_refuse_unconfigured_llm_gateway`, so that a
pod bound to a public interface with a loopback LLM gateway refuses to start rather than silently
shipping every prompt to a default. It reads the string set. The egress guard reads the parser.

Measured, they disagree on four address classes — `127.0.0.2`, `0.0.0.0`, `[::1]` and `::` — not the
two the review predicted. Driven end to end through both boot rules:

```
before   bind 0.0.0.0 + gateway on 127.0.0.2   -> BOOTS
after    bind 0.0.0.0 + gateway on 127.0.0.2   -> refused at boot
before   unauthenticated bind on 127.0.0.2     -> refused at boot
after    unauthenticated bind on 127.0.0.2     -> BOOTS
```

The first line is the hole: a pod passes the control and then fails every turn, which is the exact
outcome the control was written to prevent. The third is the same disagreement firing the other
rule where it should not.

`core/http.py` also said two safety rules asked this question. There were four callers.

## Decision

**One predicate, parsed: `core/http.is_loopback_host`.** `is_loopback_url` delegates to it,
`LOOPBACK_HOSTS` is deleted, `netguard._is_loopback` is deleted and `_check` calls the shared one.
`core.netguard → core.http` is intra-`core` and inverts no layer; `core/http.py` imports only stdlib,
and netguard arms at config import, so it must sit below.

**The unspecified address is not part of the loopback question, and dropping it was measured.**
Netguard's copy also exempted `0.0.0.0`, `::` and `""`. That cannot enter a shared answer: as a
*bind*, `0.0.0.0` is every interface, which is the entire subject of the rule it would disarm.
Before dropping it: no `0.0.0.0` URL exists anywhere in `src/`, `deploy/`, `infra/` or
`.env.example` (every occurrence is a bind); a uvicorn `0.0.0.0` bind makes zero `getaddrinfo` calls;
four different stdlib bind forms never reach `_check`; and instrumenting `_check` across the whole
suite saw eleven distinct hosts and **no** unspecified destination.

**`PG_LOOPBACK_HOSTS` stays a second constant, deliberately.** Substituting the shared predicate for
it moves behaviour in **both** directions: it widens the TLS exemption to `127.0.0.2`, the rest of
`127.0.0.0/8`, `[::1]` and `::1%lo0`; and it narrows it for the empty host, which is live — a
`file:///var/chemclaw/outbox` URL has `hostname == ""` and is exempt in `publish/drivers/http.py`
and `deliver/driver.py`, and would start demanding TLS. It asks whether a *connection is local*,
not whether a *host is a loopback address*. The argument now lives in one place, with the `""`
consequence named, and a structural test allows exactly that one second constant — so a fourth
definition fails.

## Consequences

One address table of seventeen rows drives all four callers — both boot rules, the connector
manifest and netguard — so a fifth caller cannot diverge silently. The table caught an error in its
own author's expectations: `::ffff:127.0.0.1` was written as not-loopback, and Python resolves
IPv4-mapped addresses correctly.

Three mutations go red, including a netguard that re-grows its own copy.

**The proxy row gains the half nobody had measured, and it is worse than "unbounded".** The existing
backlog row established that a *named* proxy is refused. A proxy on **loopback** needs no
allowlisting at all, because the guard exempts loopback by construction and must keep doing so — the
process dials Postgres, Temporal and the calc backend there. With the allowlist empty:

| arm | outcome | `_refused` |
| --- | --- | --- |
| `proxy=http://proxy.corp:3128` → `http://exfil.example/steal` | refused at `getaddrinfo` | 1 |
| `proxy=http://127.0.0.1:<port>` → `http://exfil.example/steal` | **HTTP 200, body returned** | **0** |
| no proxy → `http://exfil.example/steal` | refused at `getaddrinfo` | 1 |

The request reached its external destination end to end and the counter never moved, so nothing logs
at ERROR and `chemclaw_egress_refused_total` reads as a clean pod. This is the *shipped* topology
rather than an attacker-only one: an OpenShift service mesh or egress sidecar is a loopback proxy by
design, so `HTTPS_PROXY=http://127.0.0.1:15001` re-terminates TLS for every prompt, completion and
bearer token.

**It is not fixable by widening the loopback answer**, which is why it is a backlog row and not part
of this change: the guard's model is which *host* this process may dial, and a proxy moves the
destination out of the address entirely. Closing it means treating proxy configuration as a
destination, which changes behaviour for every deployment behind a legitimate mesh.
