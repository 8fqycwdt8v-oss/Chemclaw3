# Round 1 — `infra/`, `deploy/`, `.github/workflows/`, `Makefile`, `pyproject.toml`

Lens: **design and simplification**. Eight findings, each reproduced or rendered.

---

## `tblite` is a production dependency with zero runtime importers — 17.5 MB in every image

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/pyproject.toml:152` (`"tblite>=0.7.0"` in `[project].dependencies`);
  claim restated at `/home/user/Chemclaw3/src/chemclaw/connectors/calc/worker.py:5`
- **Trigger**: build the image. `deploy/Containerfile:106` runs `uv sync --frozen --no-dev`, which
  installs everything in `[project].dependencies` and nothing from `[dependency-groups].dev`. `tblite`
  is in the former.
- **Consequence**: a compiled quantum-chemistry runtime is installed into the front door, the
  background worker, all four connector-worker pods and every connector server pod, and nothing in
  `src/` imports it. `D-2026-08-16-the-physics-leaves-the-cache-stays` moved the callers to
  `Chemclaw3-mcp`; the Containerfile removed the `xtb`/`crest` *binaries* for exactly that reason
  (`deploy/Containerfile:58-68`) and the Python half was left behind. Its only remaining consumer is
  `tests/test_solvents.py`, which re-derives `ALPB_SOLVENTS` against the installed library.
- **Evidence**:

  ```
  $ grep -rn "^\s*\(from\|import\) tblite" src/ tests/
  (src: 0 hits, tests: 2 hits)

  $ uv run python -c "... for d in distributions(): who requires tblite ..."
  chemclaw -> tblite>=0.7.0        # nothing else in the closure needs it

  $ du -sh .venv/lib/python3.11/site-packages/tblite*
  7.6M  .../tblite
  32K   .../tblite-0.7.0.dist-info
  9.9M  .../tblite.libs
  ```

  Import-blocker probe (`/tmp/no_tblite.py`) — installs a `meta_path` finder that raises on
  `tblite`, then walks and imports every module under `chemclaw.`:

  ```
  modules that need tblite: []
  ```

  And the claim that contradicts this, in-tree: `src/chemclaw/connectors/calc/worker.py:5` —
  *"`tblite` and the `calc.*` closure are loaded in this process and nowhere else"*. It is loaded in
  no process. (The same docstring is also mangled across lines 3–5: *"serves\nwhatever\nimporting
  this bundle's modules registered"*.)
- **Fix**: move `"tblite>=0.7.0"` from `[project].dependencies` to `[dependency-groups].dev`.
  Behaviour-preserving: `make install`/`uv sync` and CI's `uv sync --locked` both install the dev
  group, so `tests/test_solvents.py` keeps its library; the image loses 17.5 MB it cannot use. Fix
  the `worker.py` docstring in the same commit — it is the only in-repo statement that would still
  claim otherwise. Re-run `make deps-audit` afterwards: the exported closure narrows, which is the
  point.

---

## `make live-e2e-full-stack-down` stops nothing and exits 0 when the e2e run directory is absent

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/infra/live/e2e-full-stack/up.sh:219-237` (`down()`), specifically
  the early `return` at line 221
- **Trigger**: any state where `$LIVE_DIR/e2e/run` does not exist but this repo's own lane is up —
  most plainly `make live-up` (which creates `$LIVE_DIR/run`, a *different* directory) followed by
  `make live-e2e-full-stack-down`.
- **Consequence**: `down()` logs "stopping Chemclaw3_ui", hits `[ -d "$RUN_DIR" ] || { log "nothing
  running"; return; }`, and returns **before** `bash "$REPO_ROOT/infra/live/processes.sh" down` at
  line 234. The connectors, four Temporal workers and the front door keep running and keep holding
  their ports and Postgres pool slots, while the command reports success. `status()` twelve lines
  below has the same guard written *without* the `return` — the divergence is inside one file.
- **Evidence**:

  ```
  $ export CHEMCLAW_LIVE_DIR=/tmp/audit-live
  $ mkdir -p /tmp/audit-live/run && echo 99999 > /tmp/audit-live/run/api.pid
  $ bash infra/live/e2e-full-stack/up.sh down
  [e2e] stopping Chemclaw3_ui
  [e2e] nothing running
  rc=0
  $ ls /tmp/audit-live/run/
  api.pid                       # untouched
  ```
- **Fix**: delete the early return and guard only the loop, matching `status()`:

  ```bash
  down() {
    if [ -d "$RUN_DIR" ]; then
      for pidfile in "$RUN_DIR"/*.pid; do … done
    else
      log "no external processes recorded"
    fi
    log "stopping this repo's connectors/workers/front door"
    bash "$REPO_ROOT/infra/live/processes.sh" down
  }
  ```

  Behaviour-preserving in the directory-exists case; it makes the delegating half unconditional,
  which is what the function's own log lines already promise. Move the "stopping Chemclaw3_ui" line
  down beside the loop while you are there — it currently prints before the guard that may skip it.

---

## The live lane's process harness is cloned across three scripts, and the clone dropped the fix

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/infra/live/processes.sh:32,33,81,119,204,222,246` versus
  `/home/user/Chemclaw3/infra/live/e2e-full-stack/up.sh:30,31,42,54,219,238,254`, plus
  `/home/user/Chemclaw3/infra/live/soak.sh:53` (`python_bin`)
- **Trigger**: `make live-e2e-full-stack` waits on `props` and `rxnpredict` (Chemclaw3-mcp servers,
  RDKit-class import cost) with a 120-attempt readiness budget on a cold page cache.
- **Consequence**: `start()` and `wait_for()` are byte-identical between the two files apart from a
  log-filename prefix and one number — and that number is the one `processes.sh` documents as
  *measured*:

  > "The budget is 300s, not 90. Measured: on a *cold* page cache … importing this dependency set
  > (torch, rdkit, bofire) pages in ~1 GB and the process sits in uninterruptible disk sleep for
  > minutes. At 90s the lane declared a healthy process dead and killed the run."

  The clone was written at **120**. So the four-repo lane reproduces the failure the single-repo lane
  fixed, on the two processes with the heaviest imports of the eleven it starts. Two other
  behaviours were dropped in the copy as well: the `-s` explanation and the "exited processes are
  reported as exited" comment survive only in the original, and `soak.sh` carries a third,
  byte-identical `python_bin()`.
- **Evidence**:

  ```
  $ diff <(sed -n '/^start() {/,/^}/p' infra/live/processes.sh) \
         <(sed -n '/^start() {/,/^}/p' infra/live/e2e-full-stack/up.sh)
  # differs only in the log path and two comment lines

  $ diff <(sed -n '/^wait_for() {/,/^}/p' infra/live/processes.sh) \
         <(sed -n '/^wait_for() {/,/^}/p' infra/live/e2e-full-stack/up.sh)
  2c2
  <   local name="$1" url="$2" attempts="${3:-300}"
  ---
  >   local name="$1" url="$2" attempts="${3:-120}"
  ```

  Function-for-function: `log`, `die`, `start`, `wait_for`, `down`, `status`, `restart`, `python_bin`
  — eight, ~95 lines, in two files; three of the eight have diverged (`wait_for`'s budget, `down`'s
  early return above, `log`'s stdout-vs-stderr).
- **Fix**: extract `infra/live/lib.sh` holding `log`/`die`/`start`/`wait_for`/`python_bin` with the
  colour tag and log prefix as variables (`LIVE_TAG`, `LIVE_LOG_PREFIX`), and `source` it from all
  three scripts. Behaviour-preserving except that `wait_for`'s default becomes 300 everywhere, which
  is the deliberate half of the change: the reason the number is 300 applies to `props`/`rxnpredict`
  more strongly than to anything `processes.sh` starts. `log()` goes to stderr for everyone (the e2e
  copy is the one with the argued behaviour — a stdout `log` corrupted `mock_venv_bin`'s command
  substitution; the same hazard exists in `processes.sh`'s `connector_urls`).

---

## `CHEMCLAW_CALC_SERVER_URL` is declared twice in `values.yaml`; one declaration silently wins

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/deploy/helm/chemclaw/values.yaml:335` and `:380`, both inside the
  single `config:` map
- **Trigger**: `helm template`/`helm install` with the shipped values.
- **Consequence**: Helm's YAML loader takes the last occurrence and reports nothing. Today both hold
  the same string so nothing breaks — which is exactly what makes it dangerous: an operator or a
  future edit changing the first (line 335, the one under `connectors.calc`'s cross-reference) gets
  no error, no warning, and no effect, because line 380 overwrites it. Each occurrence carries its
  own multi-line rationale comment, so a reader who finds one has no signal that a second exists.
  The `config:` block has 34 key declarations for 33 distinct keys.
- **Evidence**:

  ```
  $ grep -n "^  CHEMCLAW_CALC_SERVER_URL" deploy/helm/chemclaw/values.yaml
  335:  CHEMCLAW_CALC_SERVER_URL: "http://chemclaw3-mcp-calc:8860/mcp"
  380:  CHEMCLAW_CALC_SERVER_URL: "http://chemclaw3-mcp-calc:8860/mcp"

  $ helm template chemclaw deploy/helm/chemclaw | grep -c CHEMCLAW_CALC_SERVER_URL
  1                                    # one key rendered from two declarations, no error
  ```

  (Verified with the repo's own `helm` at `/usr/local/bin/helm` — the same binary `make helm-validate`
  shells out to. `helm template` exits 0.)
- **Fix**: delete line 335 and fold whatever is worth keeping from its comment into the line-380
  block. Behaviour-preserving (the rendered ConfigMap is byte-identical). Then add the duplicate-key
  scan to `tests/test_helm_chart.py` — five lines of `re` over `values.yaml`'s top-level maps — so the
  next one is a red test rather than a silent overwrite. `helm` itself will never report it.

---

## "Does this release run pods for this bundle" is written four times in three different ways

- **Severity**: medium
- **Location**:
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/deployment-connectors.yaml:26` and `:158` —
    `if and $cfg.server (not $cfg.url)`
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/_helpers.tpl:509` (`chemclaw.pooledProcesses`) —
    `if and $cfg.server (not $cfg.url)`
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/_helpers.tpl:470` (`chemclaw.connectorUrls`) —
    `if and $cfg.enabled $cfg.server` (no `url` test — correct there, different predicate)
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/networkpolicy.yaml:141-146`
    (`chemclaw-connector-ingress`) — `if $cfg.enabled` only
- **Trigger**: render the shipped chart, in which `chem` and `safety` carry an external `url:` and
  `qm` sets `server: false`.
- **Consequence**: the connector-ingress NetworkPolicy's `podSelector` names three component labels
  that no pod in the release ever carries. Harmless today (a selector matching nothing selects
  nothing) but it is the drift itself that is the finding: the fourth copy of the predicate stopped
  tracking the other three, and nothing renders red. The same shape in the other direction — a bundle
  that gains pods while this list does not — would leave a connector with no Ingress policy at all,
  which is the state the file's own comment says it exists to prevent ("Connectors accept traffic
  only from Chemclaw's own pods").
- **Evidence**:

  ```
  $ helm template chemclaw deploy/helm/chemclaw | grep -A9 'name: chemclaw-connector-ingress' | grep '^ *- connector'
        - connector-bo
        - connector-calc
        - connector-chem      <-- no pod
        - connector-molfp
        - connector-qm        <-- no pod
        - connector-rxnfp
        - connector-safety    <-- no pod

  $ helm template chemclaw deploy/helm/chemclaw | grep -oE "app.kubernetes.io/component: [a-z-]+" | sort -u
  … connector-bo, connector-calc, connector-molfp, connector-rxnfp,
    connector-worker-bo, connector-worker-calc, connector-worker-qm …
  ```
- **Fix**: name the predicate once in `_helpers.tpl` —

  ```
  {{- define "chemclaw.servesHere" -}}{{- if and .cfg.enabled .cfg.server (not .cfg.url) -}}true{{- end -}}{{- end -}}
  ```

  — and call it from the two `deployment-connectors.yaml` guards, `chemclaw.pooledProcesses`, and the
  NetworkPolicy range. Behaviour-preserving for the first three; for the NetworkPolicy it drops the
  three dead entries, which is the intended change. `chemclaw.connectorUrls` deliberately keeps its
  own (wider) predicate — that one is "which addresses does the front door dial", and the difference
  is argued in its comment. Making the other four one definition is what leaves that difference
  visible instead of buried among four look-alikes.

---

## The Helm hook annotation block is copy-pasted five times; the ServiceAccount copy diverges

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/config.yaml:11-14` (ConfigMap),
  `:52-54` (ServiceAccount), `:64-67` (Secret); `templates/migrate-job.yaml:9-12`;
  `templates/schedules-job.yaml:16-19`
- **Trigger**: read the three blocks in `config.yaml`, which are eleven and twelve lines apart.
- **Consequence**: the ConfigMap and the Secret both carry
  `"helm.sh/hook-delete-policy": before-hook-creation`; the ServiceAccount between them carries only
  `hook` and `hook-weight`. The file's own header comment states the reason the policy is there —
  *"`before-hook-creation` refreshes them each run"* — and then one of the three resources it
  describes does not have it. Whatever the intent, it is undocumented in a chart where every other
  choice at this level is argued in place, and it is invisible because the annotation set is written
  out longhand five times rather than named once.
- **Evidence**:

  ```
  $ sed -n '11,14p;52,54p;64,67p' deploy/helm/chemclaw/templates/config.yaml
    annotations:
      "helm.sh/hook": pre-install,pre-upgrade
      "helm.sh/hook-weight": "-10"
      "helm.sh/hook-delete-policy": before-hook-creation      # ConfigMap
  ---
    annotations:
      "helm.sh/hook": pre-install,pre-upgrade
      "helm.sh/hook-weight": "-10"                            # ServiceAccount — nothing follows
  ---
    annotations:
      "helm.sh/hook": pre-install,pre-upgrade
      "helm.sh/hook-weight": "-10"
      "helm.sh/hook-delete-policy": before-hook-creation      # Secret
  ```

  I could not exercise a `helm upgrade` here (no cluster), so I am not claiming an outcome — the
  finding is the divergence and its invisibility, both of which are on disk.
- **Fix**: add `{{- define "chemclaw.preHookAnnotations" -}}` (hook + weight, weight as a parameter)
  and `{{- define "chemclaw.hookDeletePolicy" -}}` to `_helpers.tpl`, and include them at the five
  sites. Then the ServiceAccount either has the policy or visibly does not, as a one-line decision
  rather than as three lines that happen to be two lines shorter than their neighbours.

---

## `.PHONY` is a hand-kept second copy of the target list; six targets are missing from it

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/Makefile:29` (the single `.PHONY` line) versus the 54 targets
  defined below it
- **Trigger**: a file or directory in the repository root whose name matches a target — e.g. a stray
  `upstream-check`, `share-sync` or `live-e2e-full-stack` left by a script or an editor.
- **Consequence**: make treats the target as an up-to-date file and refuses to run the recipe,
  silently and with exit 0. Missing: `upstream-check`, `share-estimate`, `share-sync`,
  `live-e2e-full-stack`, `live-e2e-full-stack-down`, `live-e2e-full-stack-status`. `upstream-check`
  is the one that matters most — its whole purpose is to be run at a dependency bump, and it would
  no-op.
- **Evidence**:

  ```
  $ python - <<'EOF'   # parse Makefile
  targets not in .PHONY: ['live-e2e-full-stack', 'live-e2e-full-stack-down',
    'live-e2e-full-stack-status', 'share-estimate', 'share-sync', 'upstream-check']
  in .PHONY but no target: []
  EOF

  $ touch upstream-check && make upstream-check
  make: 'upstream-check' is up to date.
  $ make lint                       # in .PHONY, so it still runs
  uv run ruff check .
  All checks passed!
  $ rm -f upstream-check
  ```
- **Fix**: derive the list from the same `## ` convention `help` already reads, instead of keeping a
  second copy:

  ```make
  .PHONY: $(shell grep -hE '^[a-z][a-z0-9-]*:.*?## ' $(MAKEFILE_LIST) | cut -d: -f1)
  ```

  I verified every one of the 54 targets carries a `## ` description, so this is exact, not
  approximate. Behaviour-preserving and it removes the class: a new target is phony the day it is
  written, by the same comment that documents it. The file's `help` target already makes precisely
  this argument ("a new target documents itself the day it is written and this list cannot drift") —
  the argument was just never applied to the line above it.

---

## Three chart/helper comments justify current behaviour with code that has been removed

- **Severity**: low
- **Location**:
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/values.yaml:628-631` (`securityContext.readOnlyRootFilesystem`)
    and `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/_helpers.tpl:290-293`
    (`chemclaw.podSecurityContext`) — both: *"the calculation workers shell out to xtb/crest, which
    need writable scratch"*
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/templates/_helpers.tpl:216-217` (`chemclaw.envFrom`) —
    *"The common envFrom (the whole non-secret ConfigMap) **+ the mTLS volume mount**"*
  - `/home/user/Chemclaw3/deploy/helm/chemclaw/values.yaml:574-579` (`serviceAccount.annotations`) —
    *"the pod's projected SA token can be exchanged for an Entra token (F4-T2)"*
- **Trigger**: read the comment, then read the code.
- **Consequence**: three settings are held in place by reasons that no longer exist, so nobody can
  re-open the decision without re-deriving it.
  1. Nothing shells out to `xtb` or `crest`. `deploy/Containerfile:58-68` removed the binaries; no
     module in `src/` invokes either. The stated blocker to `readOnlyRootFilesystem: true` is gone.
     (A *different*, real blocker remains and is named nowhere near the setting:
     `deploy/knowledge-sync.sh:107` writes `/tmp/chemclaw-askpass`, which fails on a read-only root
     unless `/tmp` is an emptyDir. The value's own trailing sentence gestures at it; the argued
     reason does not.)
  2. `chemclaw.envFrom` contains one thing — `- configMapRef:`. The mTLS mount is `chemclaw.tlsMount`,
     a separate define, included separately at every call site.
  3. Workload-identity federation was deleted; `src/chemclaw/core/config/entra.py:66-68` says so in
     the code. The `azure.workload.identity/client-id` annotation and the
     `azure.workload.identity/use: "true"` pod labels on four workloads
     (`deployment-service.yaml:23`, `deployment-workers.yaml:27`, `migrate-job.yaml:32`,
     `schedules-job.yaml:30`) now configure a mechanism with no consumer.
- **Evidence**:

  ```
  $ grep -rn "subprocess|which" src/ --include=*.py | grep -iE "xtb|crest"
  (no execution sites — only filename constants in artifacts.py and prose)

  $ grep -n "xtb\|crest" deploy/Containerfile
  58: # **No calculation binaries.** `xtb` and `crest` used to be installed here …

  $ sed -n '/define "chemclaw.envFrom"/,/end -}}/p' deploy/helm/chemclaw/templates/_helpers.tpl
  {{- define "chemclaw.envFrom" -}}
  - configMapRef:
      name: {{ include "chemclaw.name" . }}-config
  {{- end -}}
  ```
- **Fix**: three one-line edits, all behaviour-preserving. Restate the `readOnlyRootFilesystem`
  comment in terms of the writes that actually exist (`/tmp/chemclaw-askpass`, the uv/venv paths) so
  the knob has a real precondition an operator can satisfy. Trim `chemclaw.envFrom`'s docstring to
  what it renders. For the workload-identity annotation and the four pod labels, either delete them
  or say in one line that they are held for a re-add — a zero GUID plus a live label is
  indistinguishable from configuration that works.

---

### What I checked and did not report

- The 46 migrations: the additive/forward-only shape holds; `011`/`032`'s retired hash-chain columns
  and `audit_anchors` are dead but their retention is forced by the no-drop rule and each file says
  so. Duplicate numeric prefixes (`037`×2, `043`×2) are harmless — the ledger keys on whole
  filenames and `sorted()` is deterministic — and `infra/sql/README.md` already states it.
- `grants/app_privileges.sql`: read in full. It is one declaration, checked against the SQL literals
  in `src/` by a test in both directions. No duplication and no dead grant found beyond
  `audit_anchors`, which the file itself argues for excluding.
- `.github/workflows/ci.yml` and `image.yml`: the `deps-audit` step is genuinely the same `make`
  target in both, and `image.yml`'s smoke step derives its component list by grepping
  `entrypoint.sh` rather than restating it — both are the right shape. `pyproject.toml`'s
  `fail_under = 84` matches the comment (the earlier documented 80/84 mismatch is fixed here).
- `entrypoint.sh`, `Containerfile`: no duplicated dispatch, no hardcoded config — every port and
  bound is an env default. The `connector-worker-*` / `connector-*` case ordering is correct.
- `pooledProcesses` / `connectorUrls` / `knowledgePublishPath` / `configChecksum`: all four derive
  from the values that render the objects rather than restating them. This is the part of the chart
  that is well factored, and it is why the four-times-written connector predicate above stands out.
