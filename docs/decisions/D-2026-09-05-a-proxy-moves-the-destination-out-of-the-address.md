# D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address — the guard cannot see a proxied call

## Status

Accepted, 2026-09-05.

## The finding

`core/netguard` is an allowlist guard: it patches the socket entry points and asks "which *host*
may this process dial". A client configured with a proxy dials the **proxy** and names the real
destination in the request line, so the question the guard asks has no bearing on where the traffic
goes. Loopback makes it worse, because `_check` exempts loopback by construction and must keep
exempting it — this process dials Postgres, Temporal and the calc backend there.

Measured with the allowlist deliberately empty, one local HTTP proxy and `httpx`, reading
`netguard._refused` after each arm:

| arm | outcome | `_refused` |
| --- | --- | --- |
| `proxy=http://proxy.corp:3128` → `http://exfil.example/steal` | refused at `getaddrinfo` | 1 |
| `proxy=http://127.0.0.1:<port>` → `http://exfil.example/steal` | **HTTP 200, body returned** | **0** |
| no proxy → `http://exfil.example/steal` (the control) | refused at `getaddrinfo` | 1 |
| **no `proxy=` argument at all; `HTTP_PROXY` set on the process** | **HTTP 200, body returned** | **0** |

The fourth row is the one that matters and it is the one the backlog row did not have. It needs no
code, no explicit argument and no attacker: a plain environment variable, honoured by any client
built with httpx's default `trust_env=True`. That is the shipped OpenShift shape — a service mesh
or egress sidecar *is* a loopback proxy by design.

**And it is the one shape with no backstop below the guard.** The `netguard` module docstring
defers what it cannot cover to "the NetworkPolicy — the layer that takes the network away". A
sidecar shares the pod's network namespace, so loopback traffic to it never crosses a policy
enforcement point. For this shape the fallback does not exist.

## What made it reach every deployment

`core/http.private_ca_transport` carried `trust_env=False` and said so, in a docstring naming
exactly this attack. It also opened with `if not ca_bundle: return None`. `llm_tls_ca_bundle`
defaults to `""` and is set nowhere in `deploy/`, `infra/` or `.env.example` — so the `None` branch
is the shipped branch, both callers passed `None` to the SDK, and the SDK built its own client.

Measured on the shipped configuration, `HTTP_PROXY` pointed at a local recorder:

```
POST http://gateway.internal/v1/chat/completions
  authorization: Bearer sk-super-secr...
  body: {"messages":[{"content":"secret?","role":"user"}],"model":"mock",...}
netguard._refused delta: 0
```

on **both** `invoke` and `ainvoke`. The prompt and the gateway bearer, to a host of the env
setter's choosing, past the CA pinning (a proxy re-terminates TLS) and invisibly to the guard.

Two further things the measurement corrected, both in the reassuring direction:

- **`ChatOpenAI` was handed `http_async_client=` only.** Even on the CA branch the *sync* client —
  the one `invoke` uses — was never ours.
- **The CA branch was not protection; it was an upstream side effect.** Supplying any of
  `http_client`/`http_async_client`/`openai_proxy`/`http_socket_options` makes `langchain-openai`
  inject a socket-options transport that happens to shadow httpx's proxy auto-detection — and it
  emits a warning telling the operator how to switch that off. Measured with the bundle set and
  `LANGCHAIN_OPENAI_TCP_KEEPALIVE=0`: the sync client is back to `trust_env=True` with two proxy
  mounts. A behaviour an environment variable disables is not a control.

A comment in `deliver/driver.py` listed six clients as carrying `trust_env=False` "the same flag,
and the same reason, every other client in this tree that reaches a real dependency carries". Two
of the six carried it only on a branch nothing takes. The other four are genuine.

## The decision

**Both halves, and the guard half refuses at boot.** They close different things and neither is
sufficient.

**The client half.** `private_ca_transport` becomes `gateway_client_kwargs` and always returns
kwargs; `trust_env=False` is unconditional and the CA bundle decides only *which* certificates are
trusted, which is the opposite of how it read. Both LLM seams build both clients on every branch,
and `ChatOpenAI` gets `http_client=` as well as `http_async_client=`. The rename is not cosmetic:
the old name described the conditional half, which is how a function whose main job was the
unconditional half came to be skipped in every shipped configuration.

**`trust_env` conflates two things and only one of them is objected to.** The backlog row that
opened this named the cost and was right: `trust_env=False` also stops httpx reading
`SSL_CERT_FILE`/`SSL_CERT_DIR`, so refusing the proxy would silently swap a deployment's
env-supplied trust store for `certifi` — a second behavioural change nobody asked for, visible only
as a TLS failure against the site's own gateway. Measured with a probe bundle holding one
certificate against `certifi`'s 118:

**And getting that back wrong in the other direction is just as silent, which the first attempt
did.** Passing `create_default_context(cafile=None)` picks up `SSL_CERT_FILE`, but on the shipped
configuration — no bundle, no `SSL_CERT_*` — it falls through to OpenSSL's default paths and drops
certifi: measured, **152 certificates from the OS store against the SDK's previous 118, with 13
roots in certifi and not in the OS store**. That is a narrowing on every deployment, and it would
have shipped as a TLS failure against whichever gateway one of those 13 signed. So the precedence
is stated rather than inherited — configured bundle, else the environment's store, else certifi,
which is httpx's own order. Measured against the client the SDK used to build, in all four
combinations:

| environment | SDK (`trust_env=True`) | this function |
| --- | --- | --- |
| neither set (the shipped case) | 118 | **118** |
| `SSL_CERT_FILE` set (probe bundle of one) | 1 | **1** |
| `SSL_CERT_DIR` set | 0 | **0** |
| both set | 1 | **1** |

The trust store is therefore unchanged in every case and only the proxy is refused, which is what
lets this ADR close the row above it as well as its own.

**The guard half.** `netguard.refuse_proxied_egress` treats a proxy as a destination: if one would
carry traffic this process sends and its host is not named in `CHEMCLAW_EGRESS_ALLOW`, the process
does not start. Named
explicitly rather than accepted for being loopback, because loopback is exactly the case that needs
the operator's signature. It hangs off `arm_from_settings`, which is the one call
`chemclaw.core.config` makes, so unlike `api/middleware._refuse_unconfigured_llm_gateway` it reaches
the **durable worker** too — that guard's signal is a non-loopback *bind*, and a worker does not
bind. It runs *before* `arm()`, because a proxied call is a legitimate-looking dial to an allowed
address and arming first would mean starting a process whose guard is blind to where its prompts go.

**`NO_PROXY` is honoured, and that is what keeps this from being a nuisance.** The question is not
"is a proxy set" but "would a proxy carry traffic to somewhere this deployment actually dials", so
the destinations are `derive_allowed(settings)` and the bypass test is `urllib.request.proxy_bypass`
rather than a second reading of `no_proxy` written here — a second reading is a second answer. A
container that proxies its own package installs and excludes the cluster, which is what this
repository's CI and dev sandboxes do, is not refused.

Measured after the change:

| arm | before | after |
| --- | --- | --- |
| shipped config, `HTTP_PROXY` set, `invoke` | `'leaked'`, proxy got the prompt + bearer | `OpenAIConnectionError`, proxy got **0** requests |
| chat client `trust_env` / proxy mounts | `True` / 2 | `False` / 0 |
| no bundle, `SSL_CERT_FILE` set: CA certs trusted | 1 | 1 (unchanged — not 118) |
| boot with an undeclared proxy | boots | **refuses**, naming the proxy and the destinations |
| boot with the proxy in `CHEMCLAW_EGRESS_ALLOW` | boots | boots |
| boot with `NO_PROXY` covering every destination | boots | boots |
| boot with `NO_PROXY` missing the gateway | boots | **refuses**, naming the gateway |
| boot with `CHEMCLAW_EGRESS_GUARD_ENABLED=false` | boots | boots (one opt-out, not two) |
| no proxy configured (the common case) | boots, silent | boots, silent |

## What this costs

**A deployment behind a mesh must name its sidecar in `CHEMCLAW_EGRESS_ALLOW` or it will not
start.** That is a real behavioural change and it is the point: the operator's intent is the only
thing that distinguishes a legitimate sidecar from an env var somebody else set, and an env var
cannot carry intent. The blast radius is a site's own configuration — no shipped configuration sets
any proxy variable, verified across `deploy/`, `infra/` and `.env.example`. Outside those three
directories the mentions are comments explaining `trust_env=False` and tests that set a proxy in
order to assert something about it — including the ones this commit adds, which is worth saying
because the first draft of this sentence counted the tree *before* its own diff and claimed
there was one.

Where a proxy *is* declared, the client half is what still holds: the LLM and embedding clients
refuse the environment regardless, so declaring a sidecar for the traffic that needs it does not
hand it the prompts.

## What a fresh-context review then found in it

The commit above passed its own gate and a reviewer who did not write it found five defects in it,
three of them in the new control:

- **The boot refusal read `name` and `name.upper()` for three variables**, and every client library
  lowercases instead — so `Https_Proxy`, `HTTPS_proxy`, `https_PROXY` and `All_Proxy` each gave an
  httpx client a live proxy mount while the check saw nothing and the process booted silently. It
  now goes through `urllib.request.getproxies_environment`, which is the same source
  `proxy_bypass` reads. The rule this ADR already stated about `no_proxy` — a second reading is a
  second answer — applied verbatim to the proxy variables and had not been applied to them.
- **It charged every host in `derive_allowed` to the proxy**, including the Postgres DSN and the
  Temporal frontend. Measured: psycopg connects with all three proxy variables pointed at a dead
  port, because libpq does not read them. A site whose `NO_PROXY` covered every HTTP destination
  but not its database host would have been refused at boot by a message that was not true.
- **It ignored the scheme.** `HTTP_PROXY` alone against `https` destinations proxies nothing —
  httpx builds a plain connection pool — and the check refused anyway. The destination set is now
  `http(s)` URLs rather than bare hosts, and the proxy lookup is per-scheme.
- The trust-store narrowing above, and the JWKS client below.

The suite found one of these independently: two `tests/test_logging.py` subprocess arms, which
inherit the sandbox's own proxy variables, refused to start on an OTel endpoint. The per-scheme
narrowing fixes them for the right reason rather than by exempting the test.

Four of the new tests also passed against a gutted `refuse_proxied_egress` — they asserted "no
exception", which a deleted body satisfies. Each negative arm now carries a positive control that
re-runs the same settings with the bypass removed and asserts the refusal does fire. Against the
gutted body the file goes from 8 failures to 19.

## The guard half was aimed at the wrong destinations, and a second review found it

The first version charged every `http(s)` destination this process dials. Measured, **every
first-party client for those passes `trust_env=False`** — the gateway and embedding clients by this
ADR's own client half, the calc/rxnlabel MCP client and the connector client already — so a proxy
variable cannot carry one of them: zero proxy mounts each, against two on a default `httpx.Client`.
The control refused processes over destinations that were already immune, which is the module's own
stated failure mode ("a refusal for a reason that is not true is a pod that will not start"), and it
did so in the shipped configuration: every default destination is loopback, so on a stock checkout

```
$ HTTP_PROXY=http://proxy.corp:3128 python -c "import chemclaw.core.config"
RuntimeError: SECURITY: a proxy is configured ... would carry traffic to 127.0.0.1
```

and `pytest` collection died with it. Anybody behind a corporate proxy could not run this
repository. Meanwhile the destinations that genuinely *are* env-proxied went uncharged.

**So the question is not "which hosts does this process dial" but "which of them are dialled by
something that reads the environment"**, and the owner chose to re-target it on that basis. Today
that is two, and they disagree about the environment in a way that has to be reproduced rather than
averaged:

| reader | destination | carried by | why |
| --- | --- | --- | --- |
| the gRPC OTLP exporter | `otel_endpoint` | `grpc_proxy`, `https_proxy`, `http_proxy`, `all_proxy` | grpc resolves without consulting the target's scheme — measured, `http_proxy` alone carried a `https://` target, three `CONNECT` frames |
| the Entra JWKS fetch | `entra_jwks_url` | the destination's own scheme, or `all_proxy` | `urllib` resolves per scheme |

The exporter is the live one: with `otel_include_sensitive_data` its spans carry prompts and
completions. A bare `collector.obs:4317` endpoint — the spelling the OTLP documentation uses — is
also handled, where a scheme filter dropped it entirely because `urlsplit` reads its host as the
scheme.

**`git` is the third and is filed rather than charged.** The KG PR-gate shells out to `git push`,
which inherits the environment and is measurably proxied, but its URL is not on the settings object
(`git_remote` is the string `"origin"`): resolving it means a `git remote get-url` subprocess at
config import, a cost every entrypoint pays at every start for a destination one subsystem uses.

Two more from the same review. `carried` was keyed by destination host alone, so two *readers*
naming different proxies for one host overwrote each other — the declared one won the comparison and
the undeclared one carried the traffic, a false pass on the one question this answers. And the
re-targeting reintroduced the very bypass the first review had closed: reading the variables
directly again (necessary, because `getproxies_environment` drops `http` whenever `REQUEST_METHOD`
is set and neither git nor grpc implements that carve-out) brought back a `name`/`name.upper()`
shortcut, and measured, `Grpc_Proxy` gave grpc a live proxy while the check stayed silent.

## What is still open

- **`retrieval/vectors/qdrant.py`** builds an `AsyncQdrantClient`, which constructs its own httpx
  and takes only `verify`. It is an opt-in non-default provider (`pgvector` ships) and
  `qdrant_client` is not installed in this closure, so it is recorded rather than blind-patched.
  The boot refusal covers it in any deployment that has not declared a proxy.
- **`api/auth.py`'s JWKS client is charged but not fixed.** `PyJWKClient` fetches the tenant key
  set through `urllib.request.urlopen`, which has no `trust_env` and follows `HTTP_PROXY` —
  measured, a recorder standing in as the proxy received
  `GET http://login.microsoftonline.com/…/keys`. That is the anchor every bearer token is validated
  against. The boot refusal now names it, so a deployment cannot acquire the exposure silently; what
  is unfixed is that a site which *declares* its proxy still proxies that fetch, because there is no
  per-client seam — `urlopen` resolves from the process-global default opener, so closing it means
  `install_opener(build_opener(ProxyHandler({})))` at import, a process-wide side effect on every
  library that uses `urlopen` and a decision of its own. Filed with the measurement.

- **The dev/eval HTTP clients** (`evals/live.py`, `cli/live_probes.py`, `cli/live_storm.py`) build
  httpx clients without `trust_env=False`. They dial this system's own front door on loopback from
  a developer's machine, so they are out of the estate this ADR is about — named here so the next
  reader does not have to re-derive that.

- **A proxy set *after* arm time**, by mutating `os.environ` post-import. The check is at boot by
  design; a per-dial re-read would put an environment read on the hot path.
- **The explicit `proxy=` kwarg**, which reads no environment variable at all. No first-party client
  passes one; a library that did would be outside both halves.
- **`CHEMCLAW_EGRESS_GUARD_ENABLED=false`** disables this with the rest of the guard, deliberately.

## Anchors

`core/http.py::gateway_client_kwargs`, `core/netguard.py::refuse_proxied_egress`,
`agent/llm_provider.py::_tls_http_clients`, `core/embeddings.py::_openai_client`,
`tests/test_netguard.py`, `tests/test_llm_provider.py`.
