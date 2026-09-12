# D-2026-09-12-an-ambient-proxy-is-a-destination-nobody-declared — every outbound client in this repository refuses the environment

**Context.** `D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address` established the shape:
a client configured from `HTTP_PROXY`/`HTTPS_PROXY` dials the *proxy* and names the real
destination in the request line, so `core/netguard.py`'s allowlist — which asks only "which host
may this process dial" — sees a permitted dial, and where the proxy is a loopback sidecar (the
shipped OpenShift shape) the NetworkPolicy sees nothing either, because a sidecar shares the pod's
network namespace. `D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py` added a compiled
layer below the Python one and did not change this: an interposer on libc's `connect` sees the same
permitted dial to the same proxy. **For this one shape there is no layer below the client itself.**

That ADR's boot refusal charges only destinations reached through something that reads the
environment (`_env_reading_destinations`), and the whole correctness argument for charging two
instead of twelve is a claim about the tree: that every first-party HTTP client passes
`trust_env=False`. `tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy`
turned that claim into a ratchet and, because the claim was false when it was written, into a
ratchet with a **named exemption list of four module paths**. Three `BACKLOG.md` rows recorded what
the list was standing in for, plus two clients the ratchet could not see at all.

**Decision.** Close all three. The property this commit establishes is one sentence: *every
outbound client in this repository refuses the ambient proxy*, with no exemption and no module the
ratchet declines to look at.

## 1. The JWKS fetch — the anchor every bearer token is validated against

`api/auth.py::_client_for` builds a `PyJWKClient`, whose `fetch_data` calls
`urllib.request.urlopen`. That resolves proxies from the process-global default opener and takes no
`trust_env`; PyJWT 2.13.0's `PyJWKClient.__init__` offers `uri, cache_keys, max_cached_keys,
cache_jwk_set, lifespan, headers, timeout, ssl_context` and no opener, session or transport seam.
A proxy that could answer this fetch chooses the key set every bearer token is validated against.

Driven against a loopback recorder standing in as the proxy, five arms:

```
proxy_bypass is proxy_bypass_environment
baseline (proxy only)               recorder=['GET http://login.microsoftonline.com/…/keys']  ok
no_proxy=<host>                     recorder=[]                          PyJWKClientConnectionError
no_proxy=<host>:443 (wrong form)    recorder=['GET http://login.microsoftonline.com/…/keys']  ok
no_proxy=<url>      (wrong form)    recorder=['GET http://login.microsoftonline.com/…/keys']  ok
install_opener(ProxyHandler({}))    recorder=[]                          PyJWKClientConnectionError
```

**The fix is host-scoped, and the earlier belief that it could not be is what re-sized this row.**
`BACKLOG.md` said the only in-process route was `urllib.request.install_opener` — a process-wide
side effect on every library in the interpreter that reaches for `urlopen` — with vendoring
`fetch_data` as the alternative. `ProxyHandler.proxy_open` consults `proxy_bypass` **per request**,
so naming the host in `no_proxy` diverts this one destination and touches nothing else. It also
keeps working after the default opener has been built and cached, which is what makes it safe to do
lazily at client-build time; measured with the opener deliberately warmed through the proxy first,
the same client instance went from proxied to not proxied across the mutation.

Two traps, both driven rather than reasoned, and both are why this is not a one-line edit:

- **The form.** `proxy_bypass_environment` compares the bare host, or a dotted suffix of it. It is
  not a substring match: `login.microsoftonline.com:443` and the full URL both leave the fetch
  proxied, as the table above shows.
- **The existing value.** An operator's `no_proxy` is appended to, and **every spelling already in
  the environment** is extended. Writing lowercase `no_proxy` while the pod set `NO_PROXY` makes
  `getproxies_environment` prefer ours — its second pass overrides with the lowercase key — and
  silently drop the destinations the operator had deliberately exempted.

The mutation is per client build rather than once at arming time, because the endpoint is a
*setting*: a process that never validates a token never touches the environment, and a reconfigured
endpoint is diverted by the same call that builds its client. It is also already this module's own
vocabulary — `refuse_proxied_egress`'s refusal message tells an operator to "add these destinations
to NO_PROXY", and both sides ask `proxy_bypass`, so the boot refusal and the bypass cannot disagree
about whether the JWKS host is still carried.

**What this does not change:** the boot refusal still charges the JWKS destination, because it runs
at `chemclaw.core.config` import and the bypass is lazy. A deployment with a proxy variable and
`entra_required` on still refuses to start unless it declares the proxy or sets `NO_PROXY` itself.
That is the behaviour it had before this commit; the bypass is the layer under it, for the case
where the proxy *is* declared.

## 2. The live/eval lane — eight constructions, one carrying a bearer

Re-running the ratchet's own AST walker
(`tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy`) over this
change as it stands in PR #350 — named by the walker rather than by a branch SHA, which a squash
merge strands:

```
src/chemclaw/cli/live_probes.py:340      src/chemclaw/cli/live_storm.py:216,259,533,650,1293
src/chemclaw/evals/live.py:581           src/chemclaw/cli/phoenix_publish.py:30
```

Seven are httpx clients at the default `trust_env=True` and take one keyword each.
`cli/phoenix_publish.py:30` is **not** an httpx client — it is `phoenix.client.Client`, caught only
because the walker keys on the bare name `Client`. That accident was a true finding: the installed
SDK's signature is `(*, base_url, api_key, headers, http_client: httpx.Client | None)`, so it has no
`trust_env` of its own and its transport is the seam. It now gets
`http_client=httpx.Client(trust_env=False)`, and the walker was taught to follow an `http_client=`
delegation **by checking the delegate with the same function**, never by accepting the keyword's
name — mutated to `http_client=httpx.Client()`, the ratchet reports the site twice rather than
passing.

`_TRUST_ENV_LANE_EXEMPTIONS` is deleted, and deleting it is what closes the hole *in the ratchet
itself*. The list keyed on a **module path**, so every later client added inside one of those four
files was exempt on the day it was written — the exact property the test exists to deny. There is
now no exemption at all.

**The bearer is the one that earns a second test.** `cli/live_probes.py:340` carries
`live_probe_token` as an `Authorization` header, and an AST ratchet is a statement about the source,
not about what httpx does with it. `tests/test_live_probes.py::test_the_probe_client_does_not_hand_its_bearer_to_an_ambient_proxy`
drives the real `_client()` at a loopback recorder with the token set. Mutated — the keyword removed
— the recorder's record is `['Bearer probe-secret']`.

## 3. The Qdrant client — the one that builds its own httpx

`retrieval/vectors/qdrant.py::open_qdrant_client` passed `url`, `api_key`, `timeout` and
conditionally `verify`. The row recorded its own blocker honestly: `qdrant_client` is not in this
closure (`pgvector` is the shipped provider), so "passing a client works" would have been untested
prose. Installed in a scratch venv and measured, construction only, no server and no I/O
(qdrant-client 1.19.0, observed 2026-09-12):

```
default:                                   httpx trust_env=True
trust_env=False:                           httpx trust_env=False
AsyncApiClient accepts a prebuilt client?  False  (TypeError: unexpected keyword 'http_client')
```

The mechanism is one line of the installed source — `AsyncApiClient.__init__(self, host=None,
**kwargs)` does `self._async_client = AsyncClient(**kwargs)` — and it is why both results hold at
once: `trust_env` passes straight through, and a caller-supplied client is handed to httpx as an
unknown keyword. So the fix is `"trust_env": False` in the options dict, unconditionally.

That measurement also **spends the reason for the `verify` narrowing beside it**, which said the
keyword was passed only when configured because nothing here had run against a real client and an
unrecognised keyword would fail every deployment. Extra keywords reach httpx, and httpx takes
`verify`; the condition stays, for its own reason instead — the setting's unset value is the empty
string, which is not a CA bundle path.

**What is on this seam:** not prompts and not a bearer — it is not the LLM seam — but the embedded
text of every note indexed and every query vector searched.

## 4. The bypass raced itself, and the loser's key set came from the proxy

Pre-merge review drove `_bypass_ambient_proxy` on concurrent threads, which is how it actually runs:
`validate_token` is dispatched through `asyncio.to_thread`, so two requests bearing tokens from two
tenants genuinely build their JWKS clients at the same moment. Its body is
`os.environ[name] = f"{current},{host}"` over a `current` read a moment earlier — a non-atomic
read-modify-write of a process global. Measured with the window widened, five writers of five
distinct hosts:

```
unlocked (shipped shape)   no_proxy='tenant4.login.example'   retained 1 of 5
```

The damaging case is exactly the one this ADR is about: the loser's key set is then fetched through
the proxy, which is the control failing silently while looking installed. A module-level
`threading.Lock` around `_client_for`'s body fixes it, and the whole body is the cheapest correct
scope — holding it across a `PyJWKClient` construction costs nothing, since that constructor
performs no I/O and the key set is fetched lazily on the first `get_signing_key`. The dict beside it
(`_jwks_clients`) was unsynchronised for the same reason and is benign: racing it builds a second
client, it does not lose data.

**The test's first widener was in the wrong place and the mutation survived it**, which is worth
recording because the shape recurs: it slowed `proxy_bypass`, which runs *before* the window rather
than inside it. The widener is now a slow environment *read* that returns what it read — a first
version slept and then re-read, closing the window it existed to open, so every thread saw the value
its predecessor had just written and the unlocked code passed.

## What it cost

- **`no_proxy` gains an entry in the process environment**, which children inherit. It is a bypass,
  so it can only ever *reduce* what a proxy carries, and only for the host the deployment named as
  its identity provider. It is narrower than `install_opener`, which re-points every `urlopen`
  caller in the interpreter, and it is what `core/netguard.py` already tells operators to do.
- **`phoenix.client.Client` is now constructed with a transport this repository owns.** If that
  SDK's `http_client` parameter goes away, the publish command breaks loudly at the call rather
  than quietly reading a proxy variable.
- **Nothing measured here is a claim about a running Qdrant.** The effect is a dated observation
  against an extra outside this closure; the test can only hold that the keyword is still sent.
  That split is stated in both the test and the code comment rather than papered over.

## What keeps it true

| property | test |
| --- | --- |
| the JWKS fetch does not reach a proxy, in either scheme, with the opener already cached, and the operator's own `no_proxy` survives in both spellings | `tests/test_auth.py::test_the_jwks_fetch_does_not_follow_an_ambient_proxy` |
| every `Client`/`AsyncClient` construction in `src/` refuses the environment — **no exemptions**, and an `http_client=` delegation is followed rather than trusted | `tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` |
| the one live-lane client carrying a credential does not hand it to a proxy, driven rather than scanned | `tests/test_live_probes.py::test_the_probe_client_does_not_hand_its_bearer_to_an_ambient_proxy` |
| the Qdrant client is sent `trust_env=False`, unconditionally rather than only where a private CA is configured | `tests/test_vector_store.py::test_the_qdrant_client_refuses_the_ambient_proxy` |
| two tenants validating at once both reach `no_proxy`, so neither key set is fetched through the proxy | `tests/test_auth.py::test_two_tenants_validating_at_once_both_reach_no_proxy` |

Each was watched failing before it was kept. Twelve mutations were driven in total — the last of
them removing the lock from `_client_for`, which takes the new row from 5 of 5 hosts retained to 1 — dropping the
bypass call, writing the `host:443` form, clobbering the operator's `no_proxy`, writing only the
lowercase spelling, removing one httpx keyword, removing the Phoenix delegation, delegating to a
client that *does* read the environment, dropping `trust_env` from the Qdrant options, making it
conditional on the private CA, passing `True`, and removing the keyword from the bearer-carrying
probe client. Every one turned its test red; the two driven tests each carry a positive control
arm, because a recorder that was never wired up would prove the property by being broken.

**Superseded prose, corrected in this commit:** `core/netguard.py`'s docstring said the ratchet
"holds the lane exemptions in a named list". It does not any more, and a paragraph describing a
control this commit deleted is the failure mode this repository keeps recording — so the paragraph
was rewritten in the same diff rather than left for the next reader to disbelieve.
