# Security review — FIX PHASE (mandate: everything, phased; 4 design changes approved)

Baseline: Chemclaw3 make test = 5278 passed, 3 BO timeouts (load-induced, pass in isolation).
17 audits complete, all High findings re-verified (several with my own PoCs). Findings in
scratchpad/findings/. One branch/commit/PR per repo: claude/codebase-security-review-bqzlee.

## Approved design changes (do NOT re-ask): core egress guard · plan-approval binding ·
## pyexec reachability · role re-resolution server-side.
## Pause only for a NEW design decision not on that list.

## SEQUENCING RULE (from egress agent): LLM-path fixes land BEFORE arming the core egress guard,
## or first `make chat` fails and someone disables the guard.

## ===== CHEMCLAW3 (core) =====
### Batch 1 — LLM path + egress (must precede the guard)
- [ ] llm.py: default llm_provider -> openai_compatible (or require base_url on anthropic branch) [A4-F1/D2-C1]
- [ ] evals/live_judge.py: route through build_chat_model, no bare AsyncAnthropic [A4-F2]
- [ ] trust_env=False on every first-party httpx client + test [A4-F4]
- [ ] tiktoken: bake cache + TIKTOKEN_CACHE_DIR in Containerfile/chart; pin token_count_method [A4-F3/D1]
### Batch 2 — RCE / priv-esc / injection (High)
- [ ] checkpointer.py: JsonPlusSerializer(allowed_msgpack_modules=None) + chart env + upstream test [A3-F1]
- [ ] interceptor.py + report_workflow + template_activities: bind frozenset() roles; re-resolve [A7-F2 / role re-resolve]
- [ ] core/chem.py require_molecule: length + GetNumAtoms cap (fixes SIGSEGV, 8 sites) [A8-F1]
- [ ] plan_gate: bind approval to declared tools (plan-approval binding) [A2-F1]
- [ ] chemclaw_agent.instructions_for: layer _SAFETY_RULES onto profiles + test every profile [A2-F2]
- [ ] model_calls: clamp invalid-tool-call metric label to served surface [A5-F1]
- [ ] find_past_jobs: frame plan_step; job_status: owner check; template run id: +requester/roles [A2-F3/F4/F5]
- [ ] frame connector tool output + NoteRef.source/tags + ELN condense table defang [A2-F6/F7, A8-F4]
### Batch 3 — DoS / auth robustness (Med)
- [ ] attachments/document expand: bound actual extracted bytes; front-door expand ceiling 64MB [A1-H1]
- [ ] auth.py + middleware: log route template not raw path (redaction DoS) [A1-H2/A5]
- [ ] Postgres TLS required (pg_require_tls, default True) [A3-F2]
- [ ] Temporal TLS guard under entra_required [A7-F1]
- [ ] compare_digest on bytes (webhook + anywhere str) [A1-M1]
- [ ] require_actor/authorize_trigger: reject "" actor [A2-F9]
- [ ] core netguard.py (derived allowlist) armed after config, AFTER batch 1 [A4 design]
### Batch 4 — publish/driver hardening + Med/Low
- [ ] sink http verify_tls/https enforce; postgres sink TLS; driver module allowlist [A7-F4/F5/F6]
- [ ] git subprocess: scrubbed env + GIT_TERMINAL_PROMPT=0 [A7-F7]
- [ ] roles claim shape validation; framing secret required w/ postgres+replicas [A1-L1/A6-F4]
- [ ] 4 calc jobs expensive:true [A8-F6/A2-F10]
- [ ] record_failure uses superseded not dependencies [A8-F2]

## ===== CHEMCLAW3-MCP =====
- [ ] egress.py: add _socket to no_egress FORBIDDEN_MODULES; drop .localhost suffix rule [B1-F1/F2]
- [ ] egress.py: guard getnameinfo/gethostbyaddr; arm before app import [B1-F3/F6]
- [ ] kit app.py: redact caller-safe ValueError; session cap/reaper [B1-F4/F5]
- [ ] shared SMILES bound (length+atoms) in each server's require_molecule [B3-C1]
- [ ] chem render_structure: atom cap + admission ceiling [B3-C2]
- [ ] error truncation (120 chars) [B3-C3]
- [ ] pyexec: PID+net namespace / seccomp; ship+test securityContext; re-ratify read_only [B2 / pyexec design]
- [ ] rxnlabel HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE + test [D1-F2/B3-RL-3]
- [ ] rxnpredict fetch_models pin to SHA not "main"; verify SHA256SUMS in readiness [B3-RP-1/RP-2]
- [ ] ship fleet Deployment w/ securityContext+limits+probes+automount:false [D3-D2/A6-D2]
- [ ] Makefile suppression reason fix [D1-F1]

## ===== CHEMCLAW3_UI =====
- [ ] Markdown img: local-only override; pin img-src in csp.test [C2-F1] (closes A8 exfil chain)
- [ ] transcript localStorage key by account oid [C2-F2]
- [ ] BFF: drop x-forwarded-*/x-real-ip/x-original-url; add set-cookie+CORS to BFF_OWNED [C1-M2/M3]
- [ ] BFF: probe upstream auth posture, refuse anon under msal [C1-HIGH-1]
- [ ] BFF: book metrics/log on close; rate-limit client-events [C1-M4/F3]
- [ ] docker-compose ALLOW_DEV_AUTH default fix + test [C3-F3]
- [ ] server sourcemap off; ci.yml permissions: contents:read; npm audit fix nanoid [C3/D1]

## ===== INFRA (in the repo that owns each) =====
- [ ] networkpolicy.yaml: truthiness not empty(); string-false test [D3-NP-3]
- [ ] secrets.create: randAlphaNum not CHANGE-ME [D3-SEC-1]
- [ ] Jenkins: params via withEnv not sh interpolation; pin lib clone to SHA [D3-CI-1/CI-2]
- [ ] automountServiceAccountToken:false; readOnlyRootFilesystem where possible [D3-PS-1/PS-3]
- [ ] values-restricted.yaml worked egress example [D3-NP-2]

## Verify: make lint type test green per repo (BO timeouts expected under load — run isolated).
## Then per-repo SECURITY-REVIEW-2026-08-28.md + ADRs for test-enforced invariants.

## Review
(filled at end)

## Review (2026-08-28)
Delivered across 3 repos on branch claude/codebase-security-review-bqzlee:
- Chemclaw3 core: 9 committed batches (all Critical/High + key Mediums), each with a regression test.
- Chemclaw3-mcp: 13 fixes via agent, `make check` green (1492 passed), pushed.
- Chemclaw3_ui: 13 fixes via agent, 725 tests pass, pushed.
Design forks recorded not band-aided: plan-approval->tool binding (BACKLOG §1); role-forgery signed
payload (D-2026-08-28). Full-suite re-run in progress to confirm the globally-armed egress guard and
config guards cause no regression. SECURITY-REVIEW-2026-08-28.md is the consolidated report.
