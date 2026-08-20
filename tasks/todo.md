# Task — Prove the enforced identity path works, and close the six audit findings

Audit report: https://claude.ai/code/artifact/047c07fc-4f0a-4715-b55f-08ebc685b06b

## Plan

### Proof (F1) — the headline
- [x] Fake Entra issuer in `Chemclaw3_mock` (`app/entra/`), off by default
- [x] Backend integration test that boots the REAL app with `entra_required=true` against a real
      HTTP JWKS, nothing patched — 14 cases, mutation-proved
- [x] Live lane runs enforced (`CHEMCLAW_LIVE_ENTRA_TOKEN_URL`), and was run

### Fixes
- [x] F2 — 16 tests for `src/auth/msalAuth.ts`, five mutations caught
- [x] F3 — 401 recovery in `api/client.ts` for every route; `openStream` stops on 401
- [x] F4 — three readerless Entra settings and the `CHEMCLAW_ENTRA_TOKEN_ENDPOINT` ConfigMap value
- [x] F5 — the bundled UI mounts only when identity is off
- [x] F6a — bearer auth for `bo`, `calc`, `molfp`, `rxnfp`
- [x] F6b — `chemclaw_group_claim_overage_total` + a PrometheusRule
- [x] F6c — mock HPC compares tokens with `compare_digest` over bytes

### Close
- [x] Three ADRs + ledger rows
- [x] `make lint type test` green (4197 passed; 2 pre-existing failures need a live model key)
- [x] `BACKLOG.md` connector row and the stale live-edge lists deleted

## What was measured

**The enforced path, against the running stack** (front door, four workers, four connectors,
Postgres, Temporal), `CHEMCLAW_ENTRA_REQUIRED=true`:

| request | result |
|---|---|
| no token · garbage · unpublished key · wrong aud · wrong iss · expired · no `exp` | 401 (all seven) |
| a token the tenant vouches for | 200 |
| `DELETE /jobs/x` no roles → privileged | 403 → 404 |
| alice's session read by alice → by bench | 200 → 404 `unknown session` |
| `/healthz` `/readyz` `/metrics` → `/` | 200 → 404 |

A full turn then ran through it, and `audit_events.actor` recorded `u-alice` — the `oid` from the
minted token. That row is the chain.

**Connector credentials, from a genuinely fresh shell** after `eval "$(processes.sh env)"`:
`POST bo/mcp` 401 without, 400 with (past the gate), `GET bo/healthz` 200 unauthenticated.

**Mutation proofs.** Backend: 7 deliberate regressions, 7 caught. UI: 5 on `msalAuth`, 2 on the 401
recovery, 1 on the job stream, 3 on the proxy — all caught.

## What was *not* proven, and why

**The browser → tenant hop.** MSAL talks to `login.microsoftonline.com`; mocking that is mocking a
login UI rather than a key set, and a UI auth mode accepting tokens from an arbitrary issuer would
rebuild the `ALLOW_DEV_AUTH` hazard. The frontend's own contribution is pinned by tests instead.

**A real-model turn.** This environment's `API-KEY` is rejected by Anthropic (401 measured against
`api.anthropic.com` directly), so the live turn ran against `cli.mock_llm` — the lane's documented
credential-free mode, where only the model is faked.

## Two mutations that were invalid, recorded so nobody repeats them

- Deleting `audience=` from `jwt.decode` does not disable the audience check: PyJWT rejects *every*
  token carrying an `aud` when the decoder passes none. Use `options={"verify_aud": False}`.
- Making `IdentityProviderUnavailable` an `AuthError` subclass changes nothing — its `except` clause
  in `require_principal` is ordered first. Mutate the `raise` instead.

## Findings the work itself turned up

- **The connector probe allowlist never matched under a mount.** `request.url.path` keeps the mount
  prefix (Starlette records it in `root_path` and leaves the path whole), so `/molfp/healthz` was
  not exempt. Invisible while nothing was refused; would have 401'd the whole fleet's readiness
  sweep the day a credential was declared.
- **`--export-env` is not idempotent**, unlike the URL map it replaced: a second shell minted
  different tokens and got 401s from healthy servers. `processes.sh` persists them; `processes.sh
  env` reads them back.
- **`eval "$(...)"` swallows the exit status**, so a failing runner surfaced twenty lines later as
  `CHEMCLAW_CONNECTOR_URLS: unbound variable` instead of naming the real error.
- **The transport test rebuilt each endpoint from scratch**, dropping the manifest's `auth` — so it
  connected anonymously and proved nothing about the credential a deployment requires.
- **`--export-env` hand-quoted its values.** A minted token is base64url and could never need
  escaping; an operator-supplied one is an arbitrary string, and `processes.sh` `eval`s the output.
  `shlex.quote`, with a test that round-trips a value carrying `'` through the shell's own parser.
- **The runbook named a runtime-only path**, which `make prose-validate` resolves against the tree —
  so it passed while the lane happened to be up and failed once it was down. The subcommand is the
  contract; the file is an implementation detail and is no longer written down as one.
