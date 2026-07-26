---
name: Chemclaw Replit deployment
description: How Chemclaw3 Python/FastAPI is deployed on Replit — venv setup, port conflicts, env var bridging, Temporal dev server, ELN sync, and knowledge graph wiring.
---

# Chemclaw Replit Deployment

## Python environment
- Venv lives at `services/chemclaw/.venv`, created with `/home/runner/workspace/.pythonlibs/bin/python3.11 -m venv .venv`
- Always install with `PIP_USER=0 services/chemclaw/.venv/bin/pip install ...`
- Never use `uv run` in the workflow command — Replit's `.pythonlibs` path confuses uv's env detection
- Torch installed CPU-only first (`pip install torch --index-url https://download.pytorch.org/whl/cpu`) to prevent CUDA wheels from failing (Nix store is read-only)

**Why:** `uv sync` without a pre-created venv tries to install into `/nix/store/...` which is read-only. CPU torch must precede `bofire[optimization,cheminfo]` installation to avoid downloading 10 GB of CUDA wheels.

## Port assignment
- Port 8080 is taken by `artifacts/api-server: API Server`
- Port 8081 is taken by `artifacts/mockup-sandbox`
- Chemclaw API: **8000**, Mock backend: **8090**, Mock MCP vendor: **8091**, UI (BFF): **8099**

## Env var bridging (start.sh)
Replit AI Integration provides `AI_INTEGRATIONS_ANTHROPIC_API_KEY` and `AI_INTEGRATIONS_ANTHROPIC_BASE_URL`. The Anthropic SDK reads `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL`. The `start.sh` script maps them at runtime without touching source code.
Also bridges `DATABASE_URL` → `CHEMCLAW_POSTGRES_DSN`.

## Dev mode flags
- `CHEMCLAW_ENTRA_REQUIRED=false` — no Entra tenant
- `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` — allows binding 0.0.0.0 without Entra
- These produce a SECURITY warning in logs which is expected and harmless in dev

## Temporal
- Running as CLI dev server: `services/chemclaw/.bin/temporal server start-dev`
- In-memory store, loses history on restart — acceptable for dev
- Background worker: `services/chemclaw/start-background-worker.sh` → `python -m workers.background_worker`
- ELN sync schedule: `eln-sync`, interval 60m, task queue `background-jobs`, type `ElnSyncWorkflow`
- To create schedule after Temporal restart: `cd services/chemclaw && .bin/temporal schedule create --schedule-id eln-sync --interval 60m --type ElnSyncWorkflow --task-queue background-jobs --namespace default`
- To trigger immediately: `cd services/chemclaw && .bin/temporal schedule trigger --schedule-id eln-sync`

## Database
- Replit PostgreSQL with pgvector extension enabled
- All 14 migrations in `services/chemclaw/infra/sql/` applied via `psql $DATABASE_URL -f <file>`
- CHEMCLAW_POSTGRES_DSN is set at runtime from $DATABASE_URL in start.sh (not stored as env var since DATABASE_URL is runtime-managed)

## ELN Sync + Knowledge Graph — critical wiring

### CHEMCLAW_NOTE_REPO_DIR (REQUIRED for ELN sync)
Default `"."` is always wrong — the git submitter refuses to operate on the same checkout the service runs from (it would `git reset --hard` the working tree).

**Setup for dev:**
```bash
mkdir -p services/chemclaw-notes-repo && cd services/chemclaw-notes-repo
git init && git config user.email "chemclaw@dev.local" && git config user.name "Chemclaw Dev"
mkdir -p notes && echo "# KG" > README.md && git add README.md && git commit -m "init"
mkdir -p ../chemclaw-notes-remote.git && cd ../chemclaw-notes-remote.git && git init --bare
cd ../chemclaw-notes-repo
git remote add origin /home/runner/workspace/services/chemclaw-notes-remote.git
git push -u origin main
```
Set env var: `CHEMCLAW_NOTE_REPO_DIR=/home/runner/workspace/services/chemclaw-notes-repo`

### GraphRetriever path wiring
`GraphRetriever` reads from `settings.knowledge_dir` (default `"knowledge"`) resolved relative to the API service CWD (`services/chemclaw/`). This MUST resolve to the merged notes in the notes repo. Fix:
```bash
rm -rf services/chemclaw/knowledge   # remove pre-existing empty placeholder
ln -s /home/runner/workspace/services/chemclaw-notes-repo/knowledge services/chemclaw/knowledge
```
`knowledge_dir` must stay relative (the Pydantic validator rejects absolute paths). The symlink bridges the CWD difference.

### ELN sync PR-gate (dev workaround)
`ElnSyncWorkflow` writes each ingested reaction to a `note/<reaction-id>` branch via the git submitter. The `GraphRetriever` only reads from `main`. After ELN sync completes, merge all branches:
```bash
cd services/chemclaw-notes-repo && git checkout main
git branch | grep "note/" | tr -d ' *' | while read b; do git merge --no-ff --no-edit "$b"; done
git push origin main
```
**Why:** In production, humans review and merge each branch via PR. In dev, batch-merge to populate the knowledge graph immediately.

## Known issues filed (see ISSUES.md files in each service dir)
- `services/chemclaw-ui/ISSUES.md`: happy-dom CVE block, missing GET /sessions endpoint, missing /approvals endpoint
- `services/chemclaw-mock/ISSUES.md`: azide anion not flagged by hazard screen, CHEMCLAW_NOTE_REPO_DIR not documented as required

## How to apply future SQL migrations
```bash
for f in $(ls services/chemclaw/infra/sql/*.sql | sort); do
  psql "$DATABASE_URL" -f "$f"
done
```
