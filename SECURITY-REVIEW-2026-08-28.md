# Security review — 2026-08-28

A full-codebase, adversarial security review of the Chemclaw3 family (this repo, `Chemclaw3-mcp`,
`Chemclaw3_ui`), run as parallel scoped audits and verified by measurement rather than by reading
prose. Every finding below carries a file:line and, where the claim was that something *happens*, a
reproduction. This document records what was found in **Chemclaw3 (core)** and what changed; the
companion repos carry their own.

## The two invariants under test

1. **Egress** — nothing leaves the estate except LLM traffic through the configured gateway.
   *Result:* holds in the running system (a full agent turn, observed at the syscall level, made
   exactly one external call — the LLM — plus Postgres and the declared connectors), with named,
   now-fixed exceptions. The core process had **no** runtime egress control of its own; it does now
   (`core/netguard.py`), and the LLM path no longer defaults to the public vendor API.
2. **Ingress** — every listener is authenticated, bounded and intentional. *Result:* the front-door
   identity/authorization layer is strong (18 crafted JWTs found no bypass, no IDOR, no CSRF, no
   injection across ~150 SQL sites); the findings here were resource-exhaustion DoS, now bounded.

## Fixed in this review (Chemclaw3)

### Critical / High
- **RCE via a poisoned checkpoint blob.** The Postgres checkpointer passed no serializer, so
  LangGraph's permissive msgpack default let a stored blob name any importable `module:callable` and
  execute it on resume — reachable from the app credential's own INSERT+DELETE grant on the
  checkpoint tables. Now pinned to a strict serializer; a poisoned type is blocked, legitimate turn
  state still round-trips, and a test pins the permissive upstream default so a fix upstream turns
  red here. (`agent/checkpointer.py`)
- **The default LLM destination was the public Anthropic API.** `llm_provider` defaulted to
  `anthropic` with no base_url, so a network-exposed pod sent every prompt and completion —
  confidential chemistry — to a third-party SaaS. A boot-time guard now refuses a non-loopback
  process on that default; `trust_env=False` on every first-party client stops an ambient proxy
  capturing LLM traffic and its bearer; `evals/live_judge` no longer bypasses the provider seam.
- **A first-party runtime egress guard.** `core/netguard.py`, armed at config import, refuses an
  outbound call to a host outside an allowlist derived from the destinations the deployment actually
  dials, logs and counts it, and alerts on it. Defence in depth behind the NetworkPolicy.
- **Roles were trusted from an unsigned Temporal payload** → full impersonation. Roles now bind
  empty in the interceptor and report retrieval (actor still crosses, for attribution); the template
  authorize path keeps them, relying on broker mTLS, with the residual recorded
  (D-2026-08-28, and a signed-payload follow-up in `BACKLOG.md`).
- **An approved plan authorized *any* state-changing tool, not the plan's steps.** The clean fix is
  a harness change (the plan must enumerate its tools) rather than a patch; recorded precisely in
  `BACKLOG.md` §1 rather than shipped as a fragile prose-scan.
- **Unbounded SMILES segfaulted the worker** (uncatchable, took every concurrent session), reachable
  through read-only tools and as an ELN poison pill. Now bounded at the one shared molecule gate.
- **Six of seven agent profiles deleted the injection-defense system prompt.** A `_SAFETY_RULES`
  block (the envelope rule, `Refused:` semantics, the PR-gate, the compaction marker) is now
  appended to every profile; a test pins it for all of them.
- **Model-controlled text reached the unauthenticated `/metrics`** through an invalid-tool-call
  label — a prompt-injection exfiltration channel. The label is clamped to the served tool surface.
- **DoS:** a 1.8 MB upload could OOM-kill the 1 GiB front door (the expansion ceiling was set
  against the 4 GiB worker); the unauthenticated no-bearer path fed the raw request path to a
  superlinear redaction filter. Both bounded.
- **Transport TLS** for Temporal and Postgres is now required under the enforced posture (both
  defaulted to plaintext-or-unverified while carrying the whole system's sensitive state).
- **A quoted `allowAnyDestination` silently rendered an allow-any egress NetworkPolicy** while
  reading as off. The chart now refuses a string value.

### Medium / Low
- `record_failure(held_until=…)` silently failed to retire a refuted note (wrong PR-gate parameter);
  an empty actor was accepted as authenticated; the knowledge-merged webhook MAC is now compared as
  bytes (a non-ASCII header turned it into a 500); the four "expensive half" calc jobs now carry
  `expensive: true` so `authorize_trigger` gates them.

## Verified sound (checked, not assumed — do not "fix" these)
- No SQL injection anywhere (~150 execution sites; full-text uses `websearch_to_tsquery` as a bound
  parameter). No authn bypass, IDOR, CSRF, or open redirect at the front door. A no-env deployment
  **refuses to boot** rather than serving unauthenticated. XXE and zip-bombs are refused (proven on
  real `.xlsx`). `yaml.safe_load` everywhere. No committed live credential in any history. The KG
  PR-gate's git path is injection-safe (slug validation + `--` + resolve-after-materialize). The
  audit trail cannot be skipped and its actor is not model-writable. `hmac.compare_digest` at every
  auth boundary. No `verify=False` anywhere.

## Companion repos (summary; see each repo's own review)
- **Chemclaw3-mcp:** egress-guard bypasses closed (`_socket`, the `.localhost` suffix, reverse-DNS,
  arm-order); the same SMILES-segfault bound across four servers; the `render_structure` CPU bomb
  bounded; model-checkpoint supply chain pinned to a reviewed SHA and loaded offline; a pod
  securityContext shipped and tested for the whole fleet; `pyexec`'s false "the kill always holds"
  claim corrected and the fork-based sandbox escape closed (`process_headroom=0`, measured). Residual:
  a same-uid parent-signal and a private network stack still want a PID/net namespace the rootless
  pod is not promised — documented.
- **Chemclaw3_ui:** the model-`<img>` exfiltration channel closed and `img-src` pinned by test;
  per-account transcript storage; client-asserted `X-Forwarded-*`/CORS/Set-Cookie no longer relayed;
  a BFF upstream-auth-posture probe; aborted-request accounting; the docker-compose dev-auth bake-in
  corrected; server sourcemap dropped from the image; CI `permissions` pinned.

## Known residuals (recorded, needing a decision or infrastructure)
- Full role-scoped durable authority needs a signed Temporal payload (a codec) — see D-2026-08-28.
- Plan-approval-to-tool binding needs the harness to enumerate a plan's tools — `BACKLOG.md` §1.
- `pyexec`'s same-uid parent-signal and network isolation need a namespace the rootless pod lacks.
- The egress guard cannot see a child process, a `ctypes` call, or a compiled extension's syscalls —
  the NetworkPolicy is that layer.
