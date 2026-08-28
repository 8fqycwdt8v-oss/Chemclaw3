# Full-codebase security review (3 repos) — 2026-08-28

Scope: `Chemclaw3` (core), `Chemclaw3-mcp` (tool fleet), `Chemclaw3_ui` (frontend + BFF).
Branch in every repo: `claude/codebase-security-review-bqzlee`.

## Objectives (from the request)
1. Whole-codebase security review, maximum depth, via parallel agent teams.
2. Dependencies/libraries proven safe (SCA + supply chain + transitive telemetry).
3. **Egress invariant**: no outbound call anywhere except LLM calls through the API gateway.
4. **Ingress invariant**: no unwanted inbound reachability; every listener authenticated + bounded.
5. Harden: fix what is found; report what cannot be fixed here.

## Method
Findings are only accepted with a *file:line* and a concrete exploit path. Claims from prose
(READMEs, ADRs, docstrings) are evidence about belief, not about code — every control is checked
against the code that enforces it, and where feasible measured by running it.

## Status: infra up (dockerd + postgres/pgvector + temporal), migrations applied (62), baseline suite running.

## Status
Infra started per CLAUDE.md: dockerd + Postgres/pgvector + Temporal up, 62 migrations applied,
baseline suite running (so the Postgres-backed tests that normally skip are actually executing).
17 audit agents dispatched across waves 1 and 2.

## Wave 1 — reconnaissance & attack-surface mapping (parallel)
- [~] A1 Front-door authN/authZ (`api/auth.py`, middleware, deps, routes, rate_limit, budget)
- [~] A2 Agent authz / gates (authz, tool_authz, plan_gate, skill_access/backend, subagents, loop_cap)
- [~] A3 Data layer & injection (db, SQL sites, fulltext, vectors, migrate, retention, grants)
- [~] A4 **Egress inventory — Chemclaw3** (every network-capable import + call site)
- [~] A5 Inbound surface & deserialization (routes, SSE, ASGI, worker_http, connectors/server, paths)
- [~] A6 Secrets & configuration (config pkg, env, logging redaction, Helm/compose/Jenkins, history)
- [~] A7 Durable/Temporal + subprocess/exec + artifact store + publish sinks
- [~] A8 Ingest/KG/documents (XXE, path traversal, PR-gate integrity, indirect prompt injection)
- [~] B1 `mcp_server_kit` (bearer auth, route order, error sanitising, **egress guard bypasses**)

## Wave 2 — deep dives (parallel)
- [~] B2 `servers/pyexec` + `servers/calc` — sandbox escape, subprocess isolation, DoS
- [~] B3 remaining MCP servers — validation, pickle/model loading, manifest classification fail-open
- [~] B4 MCP fleet deployment — NetworkPolicy, Containerfiles, rootless, image supply chain
- [~] C1 UI BFF server — proxy SSRF, header/token forwarding, cookies, CSP, runtimeConfig injection
- [~] C2 UI client — MSAL, token storage, XSS, devAuth bypass, open redirect, wasm
- [~] C3 UI build/supply chain — npm lockfile, Dockerfile, vite, sourcemaps
- [~] D1 SCA across all three (pip-audit, npm audit, CVE + transitive telemetry packages)
- [~] D2 **Empirical egress test** — arm a socket guard, import/boot everything, record real connects
- [~] D3 CI/CD + IaC (Jenkinsfiles, workflows, Helm chart, k8s securityContext/RBAC/NetworkPolicy)

## Wave 3 — verification
- [ ] Adversarially verify every candidate finding; drop what cannot be demonstrated.
- [ ] Rank by exploitability x impact; assign owner repo.

## Wave 4 — hardening
- [ ] Implement fixes per repo, own branch/commit/PR, `make lint type test` (or npm equivalents) green.
- [ ] Write `SECURITY-REVIEW-2026-08-28.md` per repo; update `SECURITY.md` where posture changed.
- [ ] Record ADRs for any invariant that becomes test-enforced.

## Review
(filled in at the end)
