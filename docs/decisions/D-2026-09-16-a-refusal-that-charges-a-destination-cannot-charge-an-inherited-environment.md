# D-2026-09-16-a-refusal-that-charges-a-destination-cannot-charge-an-inherited-environment — the boot refusal gains a second arm

**Context.** `D-2026-09-12-an-ambient-proxy-is-a-destination-nobody-declared` closed the client
half of the proxy problem: every outbound client in `src/` now passes `trust_env=False`, including
the Entra JWKS fetch, which moved from PyJWT's `urllib.request.urlopen` to `_HttpxJwkClient`. That
premise is sound and was re-driven here — the httpx control and upstream `PyJWKClient` both went to
the recorder standing in for a proxy; this tree's client went to the origin.

Because the JWKS destination became immune, its row had to leave `_env_reading_destinations`: what
that function feeds is a **refusal**, and a refusal for a reason that is no longer true is a pod
that will not start. That deletion was right and stays.

**What it did to the refusal as a whole was not seen.** The JWKS row was, for an
`entra_required=true` + `otel_enabled=false` configuration, the *only* destination charged at all.
Measured on the commit that removed it, with `HTTPS_PROXY=http://sidecar.internal:15001`:

```
charged: []
proxied_destinations: {}
BOOT: proceeds
```

On the parent commit the same settings refused, and the test that had asserted so
(`test_the_jwks_fetch_is_charged_by_its_own_scheme`) was deleted with the row.

**Why that matters more than an empty list.** `kg/git_writer._git_child_env` deliberately keeps
*any* proxy in the `git` child's environment (its own docstring says so, and it is measured here:
`HTTPS_PROXY` survives it while `CHEMCLAW_LLM_API_KEY` is scrubbed). The `git` destination is
explicitly **not** charged — `git_remote` is the string `"origin"`, so its host is not on the
settings object. And `core/netguard_preload.c` exempts loopback by construction. So on a site with
identity enforced, OTEL off, behind an OpenShift egress sidecar on loopback, the knowledge-graph
note push and its credential travelled with no layer able to observe them. The shipped Helm chart
sets `CHEMCLAW_OTEL_ENABLED: "true"`, so it still refused — but `make chat`, `make connectors`, CI
and a hand-started worker do not, which is the "a policy only one way of starting the process
obeys" shape `core/egress.py` exists to reject.

Measured with `git ls-remote https://notes.example.invalid/...` behind a loopback recorder:
`CONNECT notes.example.invalid:443`. The carrier is real, not argued.

## Decision

`refuse_proxied_egress` gains a **second arm**: under `entra_required`, an *undeclared* ambient
proxy variable is itself the refusable condition, with no destination needed.

**Option (a) — charge another destination — was measured and is unavailable.** The question was
whether an Entra-enforced process dials anything else that reads the environment. It does not: the
JWKS endpoint is the only destination `entra_required` implies, there is no MSAL, no OIDC discovery
and no token-endpoint dial in `src/`, and `entra_issuer_url` is a string compared against a claim
rather than an address. A row needs a host to put in its message and to test `proxy_bypass`
against, and there is none.

**Option (b) is what ships**, narrowed three ways so it is not a ban on proxies:

* **Gated on `entra_required`**, this repository's existing signal for "the deployment that
  believes it is in the enforced posture" (`publish/drivers/http.py`,
  `publish/drivers/postgres.py`, the broker-TLS and DSN-`sslmode` refusals in `core/config`). A
  stock checkout behind a corporate proxy must still import — that is a measured requirement here,
  not a courtesy, and `test_the_shipped_defaults_start_behind_a_corporate_proxy` holds it.
* **`egress_allow` naming the proxy clears it**, the same escape the charged arm takes: naming the
  sidecar is what distinguishes an operator's intent from a variable somebody else set.
* **`NO_PROXY=*` clears it**, because it is measured to work on the carrier this arm is about:
  with it set, the same `git ls-remote` resolved the host directly and the recorder saw nothing. A
  *per-host* `NO_PROXY` cannot clear it and is deliberately not offered in that message — the
  destinations have no names here, so offering it would be a remedy that does not work.

The two arms raise separately because their remedies differ; both messages open on the same string
so an operator greps one thing.

**What this does not resurrect.** The refusal charged here is not the JWKS fetch, and that row
stays deleted — asserted as an absence, now with a positive control, since the version of that
absence test that shipped passed against an `_env_reading_destinations` whose body had been
deleted.

**Measured matrix after the change:**

| configuration | before | after |
| --- | --- | --- |
| `entra_required`, otel off, non-loopback proxy | proceeds | **refused (ambient)** |
| `entra_required`, otel off, loopback sidecar proxy | proceeds | **refused (ambient)** |
| `entra_required`, otel on, proxy | refused (charged) | refused (charged) |
| `entra_required`, proxy named in `egress_allow` | proceeds | proceeds |
| `entra_required`, proxy + `NO_PROXY=*` | proceeds | proceeds |
| `entra_required`, no proxy | proceeds | proceeds |
| dev defaults + corporate proxy | proceeds | proceeds |

**Superseded prose, corrected in this commit.** `core/netguard.py`'s module docstring said the
shipped chart's `CHEMCLAW_ENTRA_REQUIRED: "true"` was why the refusal fired in the OpenShift
topology the sidecar argument is about. That causal claim was false in the commit that wrote it:
`entra_required` charged nothing, and the only thing still firing there was the chart's unrelated
`CHEMCLAW_OTEL_ENABLED`. It is now true, and the docstring says which arm makes it true.

## What keeps it true

| property | test |
| --- | --- |
| the enforced posture behind an undeclared proxy is refused, and it is the ambient arm rather than the JWKS row having come back | `tests/test_netguard.py::test_the_enforced_posture_is_refused_behind_an_undeclared_proxy` |
| every way out of that arm works — `egress_allow`, `NO_PROXY=*`, identity off, no proxy at all — with a positive control so the four negatives mean something | `tests/test_netguard.py::test_the_ambient_arm_is_the_enforced_posture_and_not_a_proxy_ban` |
| the JWKS row stays out of `_env_reading_destinations`, against a control that fails if that function is gutted | `tests/test_netguard.py::test_the_jwks_fetch_is_no_longer_a_charged_destination` |
| a declared proxy on one reader does not mask an undeclared one on another *inside the refusal*, with the both-declared control | `tests/test_netguard.py::test_two_readers_on_one_host_do_not_collapse` |
| a stock checkout behind a corporate proxy still imports | `tests/test_netguard.py::test_the_shipped_defaults_start_behind_a_corporate_proxy` |
| a module-level `httpx.get`/`post`/`stream` is in the `trust_env` ratchet, so the premise this decision rests on cannot decay by one more name | `tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` |

Each was watched failing before it was kept: gutting `_env_reading_destinations` turns the absence
test red (it was green against that mutation before), emptying `ambient_proxies` turns both new
tests red, dropping the `undeclared` filter turns the two-readers test red, and collapsing
`proxied_destinations`' key turns it red for the reason it was written.
