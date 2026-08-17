# Verdicts — `api-auth--security.md`, reachability lens

Scope: findings marked **critical** or **high**. The file has exactly one — finding 1. Findings 2
(medium), 3 (medium), 4 (low) and 5 (low) are out of scope and were not judged.

Working tree was clean in `src/` for the whole run (`git status --short` shows only untracked
`tasks/audit-2026-08-16/**`), so no diff against the pristine copy was needed.

---

## 1. An unauthenticated caller drives one outbound request to the tenant IdP per HTTP request, and starves the process-wide thread executor while doing it

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Everything below is end-to-end through the real `create_app()` / real `require_principal`, with a
real TCP listener standing in for the tenant JWKS endpoint so outbound fetches are counted at the
socket, not mocked. Scripts under
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/`.

**A. Mechanism of the un-cooled fetch (`e2e_amp.py`, real FastAPI `TestClient`, `POST /sessions`,
the finding's own 70-byte token):**

```
IdP returning 500: 50 HTTP requests -> 50 outbound JWKS fetches in 0.11s
  status codes: {503: 50}
IdP healthy:      50 HTTP requests -> 2 outbound JWKS fetches
  status codes: {401: 50}
```

Confirmed at the library level too — PyJWT 2.13.0 `PyJWKClient.fetch_data` writes
`self.jwk_set_cache.put(...)` only after a successful `urlopen`, so a failing endpoint leaves the
cache permanently empty.

**B. Executor starvation, isolated by cache state (`e2e_warm.py`). Same endpoint, healthy first to
warm the cache, then switched to blackhole (accept, never answer). 64 concurrent unauthenticated
garbage-`kid` requests; a real RS256 token on the cached `kid` and an unrelated `asyncio.to_thread`
call are measured mid-flood:**

```
workers: 8
warm-up (healthy): 200 legit-user   fetches: 1

--- WARM CACHE, IdP blackholed ---
  unrelated to_thread latency during flood: 0.00 s
  legit user's request during flood: 200 legit-user in 0.00 s
  attacker outcomes: {'503': 1, '401': 63}   outbound fetches: 1

--- COLD CACHE (pod restart / >300s lifespan), IdP blackholed ---
  unrelated to_thread latency during flood: 79.58 s
  legit user's request during flood: 503 in 10.01 s
  attacker outcomes: {'503': 64}             outbound fetches: 65
```

**C. Same script with the IdP left healthy (`e2e_coldhealthy.py`) — i.e. the "or simply a cold
process" leg of the trigger:**

```
--- COLD CACHE, IdP healthy ---
  unrelated to_thread latency during flood: 0.00 s
  legit user's request during flood: 200 legit-user in 0.00 s
  attacker outcomes: {'401': 64}   outbound fetches: 9
```

**D. Blast radius of the starvation (`e2e_starve.py`):** `cpu_count=4`, default executor
`max_workers=8`; baseline unrelated `to_thread` 0.39 ms → 79.57 s under 64 in-flight garbage
tokens; **event-loop hop while starved: 0.181 ms**.

**E. Upstream admission control:** `grep -rn "client.host\|X-Forwarded-For\|remote_addr" src/`
returns nothing, and `deploy/helm/chemclaw/templates/service-route.yaml` carries no HAProxy
rate-limit annotation (`route.ipWhitelist` is empty by default). Nothing stands between the
internet and `require_principal` but `--limit-concurrency 256`.

### Why

The mechanism is real and my measurements are harsher than the reporter's in one place (79.6 s, not
9.01 s — 64 requests over 8 workers is eight rounds of the 10 s timeout, and their probe evidently
sampled during the first round). Point E holds exactly as written: there is no pre-auth admission
control anywhere. So this is not a refutation of the code reading.

It is a refutation of the **trigger** and of the **consequence**, on three counts.

**1. The trigger's "or simply a cold process" is false.** Run C: cold cache, healthy IdP, 64
concurrent unauthenticated requests → **9** outbound fetches (one per executor worker, then the
cache is warm), zero starvation, legit user answered in 0.00 s. A cold process costs a handful of
fetches once, not one per request. The only real precondition is a *failing* IdP.

**2. In the normal steady state the cooldown does exactly what its comment claims, and the
finding's headline number does not reproduce.** Run B, warm cache: 64 unauthenticated garbage-`kid`
requests produce **1** outbound fetch, 63 immediate 401s with no network at all, unrelated
`to_thread` latency **0.00 s**, and a legitimate user is served in 0.00 s. The finding asserts the
comment at `auth.py:71-77` is contradicted and that "the stated failure … is still exactly what
happens" — but the failure that comment names (unknown `kid` → forced re-fetch per request) is
*fixed*, and my warm-cache run is the measurement that shows it. The genuine gap the finding found
is the `get_signing_keys()` call on line 132, which only reaches the network when the cache is
empty — i.e. when the IdP is already failing. That is a narrower defect than the section title.

**3. The starvation is reachable only in a state where the front door is already 100% down, and
only for one IdP failure mode.**
- *Failure mode.* A fast-failing IdP — HTTP 500, connection refused, DNS NXDOMAIN, TLS reject, which
  is most of what "degraded" means in practice — produces **no starvation at all**: run A did 50
  fetches in 0.11 s. Starvation needs an IdP that accepts the TCP connection and never answers,
  burning the full `entra_http_timeout_seconds`.
- *Already-down.* In the one state where it does reproduce (cold cache + blackhole), run B shows the
  legitimate user gets **503 in 10.01 s** — and that is with the attacker's contribution being
  queueing only. With zero attackers, an empty key set means *no* token can be validated, so every
  caller is 503 regardless. The attacker does not convert a working service into a broken one; they
  add latency to a service the IdP outage has already taken offline. And they are not even the
  necessary condition: eight ordinary authenticated users hitting the same window each hold a worker
  for 10 s and saturate the pool by themselves.
- *Blast radius is narrower than "every unrelated in-process worker call" implies.* The event loop
  stays responsive (0.181 ms hop under full starvation), so `/healthz`, `/readyz`, `/metrics` and
  the byte-streaming of open SSE connections are untouched. What is delayed is the `to_thread`
  callers — `build_graph`, `embed_texts` — inside turns that were authenticated before the outage
  began. That is the residual real harm and it is worth fixing; it is not a front-door outage
  caused by an anonymous caller.

**On the "retry storm against the IdP" framing.** The ratio measured in run A is exactly **1:1** —
one inbound HTTP request, one outbound JWKS fetch. This is pass-through, not amplification. An
anonymous caller wanting to send N requests at `login.microsoftonline.com` can send them directly,
without ChemClaw and without a 256-connection ceiling per pod. The distinctive cost is our own
egress and thread budget, which is claim (b), already addressed above. (One thing the reporter did
not raise that would *strengthen* the egress half if a deployment has it: with
`networkPolicy.egressDestinations` empty by default and 6 replicas × 256 connections, a corporate
forward proxy in front of Entra could see ~1500 in-flight connections from this namespace during an
outage. That is speculative — no proxy is configured in the chart — and it still requires the IdP
outage, so it does not move the verdict.)

**Severity.** Medium, not high. Fix #2 in the finding (a dedicated bounded executor for validation)
is cheap, correct, and I would take it — the coupling between "the IdP stalls" and "the KG loader
and the embedder queue" has no reason to exist. Fix #1 (negative-cache the failed fetch behind the
same cooldown) is also right and would collapse run A's 50 fetches to 1. But the finding's high
rating rests on "an unauthenticated caller degrades a healthy deployment", and the measurements say
the healthy deployment is unaffected (run B warm: 0.00 s, 1 fetch) while the degraded one is
degraded by the IdP rather than by the caller.
