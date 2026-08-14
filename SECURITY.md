# Security posture

This documents what Chemclaw enforces today, what is gated on live infrastructure, and how to
deploy it safely. It reflects the **F4 identity/RBAC** foundation (Entra everywhere); it is not the
pre-Phase-6 "no auth" world. For the design rationale see `docs/reference/architektur.md` §7/§8 and
`docs/decisions/` D-042…D-047, D-052.

## What is enforced

- **Front-door authentication (Entra OIDC).** Every non-health request to the run service carries an
  Entra-issued token that `api/auth.py::validate_token` verifies: RS256 signature against the
  tenant JWKS, **audience** (`entra_audience` — the confused-deputy guard, since the front door is
  both an OAuth client and a protected resource), issuer, and a required `exp`. The claims become a
  `Principal` (`oid`/`upn`/roles) that attributes and authorizes every backend action.
- **The reject-if-absent rule.** Every *user-triggered* durable workflow is user-specific:
  `agent/authz.py::require_actor` returns the turn's Entra `oid` and, under enforcement, **rejects a
  trigger with no authenticated user** before any durable work starts. The `oid` is stamped into the
  workflow payload (`requested_by`), never inferred later.
- **One authorization gate for expensive actions.** `agent/authz.py::authorize_trigger(action)` is
  the single place a costly HPC/BO trigger is checked: a job a connector manifest declares
  `expensive: true`, or an action named in `entra_expensive_actions`, runs only for a caller holding
  one of `entra_privileged_roles` — and an empty role set refuses everyone rather than admitting
  them. The manifest declaration is the source; `entra_expensive_actions` adds to it for anything
  outside a bundle. This holds even when the harness plans
  autonomously — an autonomously-planned todo cannot launch a job outside the requesting user's
  entitlements.
- **Role-scoped skills.** `agent/skill_access.py::RoleScopedSkillsSource` hides a gated skill
  (`skill_role_gates`: skill → allowed roles) from a caller holding none of its roles (D-052).
- **Ambient identity, one carrier.** The runner stamps the validated identity into
  `src/chemclaw/core/identity_context.py` (a task-local `contextvar`); audit, the authz gate, job attribution,
  and skill scoping all read it there, so concurrent turns never cross identities.
- **The audit trail.** `src/chemclaw/agent/audit.py` logs every agent tool call once (correlation id, actor,
  truncated args, outcome, latency) via a single middleware, with an optional append-only Postgres
  `audit_events` sink (default log-only).
- **The PR-gate.** Anything the agent generates (job results, notes, reports, distilled playbooks)
  enters the knowledge graph only through a human-reviewed pull request. The agent can *propose*
  truth; it cannot *merge* it — the agent proposes, a human decides.
- **Transport identity (non-Entra bridges).** Identity rides *inside* the workflow payload, so the
  transports are authenticated separately: Temporal by mTLS (`temporal_tls_*`) or a Cloud API key,
  and the HPC launcher by a bridged/mounted token (F4-T6). Backend pods mint their own short-lived
  Entra tokens via **workload identity federation** (`src/chemclaw/agent/identity/workload.py`) — no client
  secret at rest.

## Data handling & logging (PII in the audit trail)

The audit trail records **who ran what**: alongside the actor's Entra `oid`, it stores each tool
call's *arguments*, which are user free text (a chemist's message, a confirmed-answer payload) and so
may contain PII or confidential chemistry. This is intentional — the trail exists to be an
attributable "who did what to which inputs" record — but it has data-handling consequences:

- **Bounded, not omitted.** `agent_audit_max_arg_chars` (default 200) caps how much of each argument
  is stored, so a record cannot balloon, but the excerpt is still real user content.
- **Two sinks.** The stdlib log is always written; the append-only Postgres `audit_events` sink is
  optional. A deployment's **log retention, access control, and PII policy must cover both** — the
  audit trail is subject to the same data-protection rules as any store of user content.
- **Client-facing surfaces do not leak it.** Turn errors return a generic, session-keyed message
  (the detail is logged server-side only), token-validation failures return a generic 401, and
  upstream response bodies are bounded before they reach a log — so the trail is the *deliberate*
  place user content is retained, not an accidental one. An **unreachable tenant JWKS answers 503**,
  not 401: it is our outage rather than the caller's bad credential, and reporting it as a rejected
  token both misinforms the user and files a dependency failure under "someone is probing us".

## Front-door hardening

The browser-facing run service sets `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, and HSTS (config-gated by `service_security_headers`, default on); bounds
each chat message (`service_max_message_chars`) and the live-session cache
(`service_max_live_sessions`); and **refuses to start** if it would run unauthenticated
(`entra_required=false`) on a non-loopback bind — `service_allow_insecure=true` is the explicit,
named opt-out. That combination used to warn and boot, which left a network-exposed deployment with
every authorization gate open one missed log line away (SEC-2). It does not serve an OpenAPI schema,
Swagger or ReDoc page: all three are plain routes no dependency can gate, and the schema alone
documents every route, parameter and model the service has.
See `docs/archive/audit/` for the audit that added these.

## The enforcement switch

`entra_required` gates enforcement centrally:

- **`entra_required=true`** (every real deployment): a missing/invalid token is a 401, `require_actor`
  rejects an absent user, and `authorize_trigger` applies. Set the tenant/client/audience alongside.
- **`entra_required=false`** (local dev only, no tenant): a fixed dev `Principal` stands in, the
  authz gates are open, and user-triggered workflows attribute to `service_actor_id`. **Never run a
  shared or exposed deployment in this mode.** The testing CLI's `--admin` bypass (`src/chemclaw/cli/chat.py`)
  is a dev-only convenience and inherits this caveat.

The raw LLM inference credential is the one deliberate exception to Entra: it is a single generic API
key (the model call is not a user-scoped resource), not a per-user token.

## Live edges still open

The code paths exist and are unit-tested against local keys/fakes, but the following need real
infrastructure to exercise end to end and must be validated in a staging tenant/cluster before
production (tracked in `docs/planning/BACKLOG.md`):

- Real Entra token validation against a live tenant JWKS; the federation and On-Behalf-Of
  (`src/chemclaw/agent/identity/obo.py`, currently dormant) token exchanges.
- Temporal broker mTLS/API-key transport and the HPC identity bridge against real endpoints.
- Live-cluster delivery: `helm`/`kubeconform` render, the NetworkPolicy ingress gate, and durability
  under a self-hosted Temporal.

## Autonomy note

`harness_autonomy` defaults to `plan_only` (approval-first: the agent presents a plan before
executing). `execute` lets the agent work through its todo list autonomously, chaining tool calls —
including durable/expensive jobs — without a human turn between them, bounded by
`harness_max_loop_iterations`. Expensive actions remain gated by `authorize_trigger`, but review the
exposure and entitlements before enabling `execute` for anything beyond a single trusted operator.

## Reporting

This is a research prototype, not a published product. Report a suspected vulnerability privately to
the maintainers rather than opening a public issue.
