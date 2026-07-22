# Security posture

This documents what Chemclaw enforces today, what is gated on live infrastructure, and how to
deploy it safely. It reflects the **F4 identity/RBAC** foundation (Entra everywhere); it is not the
pre-Phase-6 "no auth" world. For the design rationale see `docs/architektur.md` §7/§8 and
`DECISIONS.md` D-042…D-047, D-052.

## What is enforced

- **Front-door authentication (Entra OIDC).** Every non-health request to the run service carries an
  Entra-issued token that `service/auth.py::validate_token` verifies: RS256 signature against the
  tenant JWKS, **audience** (`entra_audience` — the confused-deputy guard, since the front door is
  both an OAuth client and a protected resource), issuer, and a required `exp`. The claims become a
  `Principal` (`oid`/`upn`/roles) that attributes and authorizes every backend action.
- **The reject-if-absent rule.** Every *user-triggered* durable workflow is user-specific:
  `agents/authz.py::require_actor` returns the turn's Entra `oid` and, under enforcement, **rejects a
  trigger with no authenticated user** before any durable work starts. The `oid` is stamped into the
  workflow payload (`requested_by`), never inferred later.
- **One authorization gate for expensive actions.** `agents/authz.py::authorize_trigger(action)` is
  the single place a costly HPC/BO trigger is checked: an action in `entra_expensive_actions` runs
  only for a caller holding one of `entra_privileged_roles`. This holds even when the harness plans
  autonomously — an autonomously-planned todo cannot launch a job outside the requesting user's
  entitlements.
- **Role-scoped skills.** `agents/skill_access.py::RoleScopedSkillsSource` hides a gated skill
  (`skill_role_gates`: skill → allowed roles) from a caller holding none of its roles (D-052).
- **Ambient identity, one carrier.** The runner stamps the validated identity into
  `agents/identity_context.py` (a task-local `contextvar`); audit, the authz gate, job attribution,
  and skill scoping all read it there, so concurrent turns never cross identities.
- **GxP audit trail.** `agents/audit.py` logs every agent tool call once (correlation id, actor,
  truncated args, outcome, latency) via a single middleware, with an optional append-only Postgres
  `audit_events` sink (default log-only).
- **The PR-gate.** Anything the agent generates (job results, notes, reports, distilled playbooks)
  enters the knowledge graph only through a human-reviewed pull request. The agent can *propose*
  truth; it cannot *merge* it — the "AI proposes, human signs off" GxP line.
- **Transport identity (non-Entra bridges).** Identity rides *inside* the workflow payload, so the
  transports are authenticated separately: Temporal by mTLS (`temporal_tls_*`) or a Cloud API key,
  and the HPC launcher by a bridged/mounted token (F4-T6). Backend pods mint their own short-lived
  Entra tokens via **workload identity federation** (`agents/identity/workload.py`) — no client
  secret at rest.

## The enforcement switch

`entra_required` gates enforcement centrally:

- **`entra_required=true`** (every real deployment): a missing/invalid token is a 401, `require_actor`
  rejects an absent user, and `authorize_trigger` applies. Set the tenant/client/audience alongside.
- **`entra_required=false`** (local dev only, no tenant): a fixed dev `Principal` stands in, the
  authz gates are open, and user-triggered workflows attribute to `service_actor_id`. **Never run a
  shared or exposed deployment in this mode.** The testing CLI's `--admin` bypass (`agents/cli.py`)
  is a dev-only convenience and inherits this caveat.

The raw LLM inference credential is the one deliberate exception to Entra: it is a single generic API
key (the model call is not a user-scoped resource), not a per-user token.

## Live edges still open

The code paths exist and are unit-tested against local keys/fakes, but the following need real
infrastructure to exercise end to end and must be validated in a staging tenant/cluster before
production (tracked in `BACKLOG.md`):

- Real Entra token validation against a live tenant JWKS; the federation and On-Behalf-Of
  (`agents/identity/obo.py`, currently dormant) token exchanges.
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
