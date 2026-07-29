# D-147 — The packages regrouped under `src/chemclaw/` by the four architecture layers

Eighteen flat top-level Python packages — `agents bo calc chemclaw connectors eln evals kg
mcp_servers memory report safety scripts service sources templates workers workflows` — with no
stated grouping, sitting beside the data corpora, the docs, the tests and the deploy tree. Nothing
in the layout said which of them were the four layers `CLAUDE.md` opens with, which was the shared
kernel, and which were ops tooling. Several were near-homonyms of each other:

- `chemclaw/` inside repo `Chemclaw3` (and, until D-145, inside `services/chemclaw/`) — the same
  word at three levels, naming something that is neither the repo nor the service but the *shared
  kernel*: config, db, http, ids, logging.
- `calc/` beside `connectors/calc/`, `bo/` beside `connectors/bo/`, `safety/` beside
  `connectors/safety/` — engine and wrapper, indistinguishable by name.
- `workflows/` beside `.github/workflows/` beside `connectors/*/workflows.py`.
- `workers/` (one 60-line module) beside `connectors/*/worker.py`.
- `service/` one character from `services/`.
- `mcp_servers/molfp` beside `connectors/molfp`.

## The layout

One package, `src/chemclaw/`, with subpackages named for what they are:

| New | Was | |
| --- | --- | --- |
| `core/` | `chemclaw/` | the shared kernel |
| `agent/`, `api/` | `agents/`, `service/` | layer 1 (MAF) and the HTTP front door |
| `durable/` | `workflows/` + `workers/` | layer 2 (Temporal) |
| `connectors/` | `connectors/` | the capability seam, bundles unchanged inside |
| `science/{calc,bo,safety}` | `calc/`, `bo/`, `safety/` | the pure-computation engines |
| `kg/`, `ingest/{sources,eln}`, `retrieval/`, `memory/` | `kg/`, `sources/`+`eln/`, `report/`, `memory/` | layer 4 and what feeds and reads it |
| `mcp/`, `templates/`, `evals/`, `cli/` | `mcp_servers/`, `templates/`, `evals/`, `scripts/`+`agents/cli.py` | the remainder |

**The `src/` layer is the part that makes the root readable**: `src/` is all the code, and
everything beside it — `knowledge/`, `skills/`, `profiles/`, `templates/`, `evals/`, `data/`,
`docs/`, `tests/`, `deploy/`, `infra/` — is data, configuration or documents. That is a rule a
newcomer can hold in one sentence, which the flat list never was.

`science/` is the rename that earns the most. `calc` and `connectors/calc` read as a duplication;
`science/calc` and `connectors/calc` read as a pair, which is what they are — the engine is pure
computation with no Temporal or MCP import, the connector is the durable-job and tool-surface
wrapper around it. **They were deliberately not merged.** Merging would put orchestration imports
inside the physics and break the layering the tests guard; the problem was never the split, it was
that the names hid it.

`durable/` merges `workflows/` and `workers/` because the second was a single module whose whole job
was to serve what the first declared, and because `workers/` was easy to confuse with the
per-bundle `connectors/<name>/worker.py`, which is a genuinely different thing.

## What this removes rather than detects

`tests/test_packaging.py` existed because three hand-kept lists of the eighteen packages — `make
type`, the wheel's `packages`, coverage's `source` — had all silently drifted, omitting
`connectors/` (the entire capability surface) and `templates/` from the shipped wheel while
`pyproject.toml` stated in prose the invariant it was violating (D-117). The `Containerfile` carried
a fourth such list, twenty-five `COPY` lines. `tests/test_no_egress.py` and `tests/test_publish.py`
each carried a fifth and sixth.

All six are now one name. The test remains, but its job changed from reconciling lists to holding
the layout: `src/` contains exactly one package, and **no top-level import package may reappear
beside it** — which is both how the eighteen accumulated and a real hazard, since a directory
importable from the repository root shadows the installed package for any process started there.

`tests/test_layering.py` gains the rule the flat layout could only imply: **`chemclaw.core` imports
no sibling.** It holds today across all eleven kernel modules and twelve siblings. A kernel that
reaches back up into a layer above it is how the first import cycle formed (`agents` ↔ `report`, via
an embedding seam that now lives in `core.embeddings`), and it is far easier to do by accident when
the thing you want is one `from chemclaw.…` away.

## The one behaviour change

`connectors_dir`, `data_sources_dir` and `safety_rules_path` defaulted to the CWD-relative strings
`"connectors"`, `"sources"` and `"safety/rules.yaml"`. They now resolve against the installed
package (`config._shipped`).

This is a fix, not a consequence. Those defaults only ever worked when the process happened to be
started from the repository root — which is precisely why the `Containerfile` had to COPY the
bundles into the workdir rather than just installing the package, and why running the front door
from anywhere else silently discovered **zero** connectors rather than failing. Verified directly:
`uvicorn chemclaw.api.app:create_app` started with the working directory outside the repository now
serves and discovers all seven bundles. The env vars still override, and both directory settings
remain `PATH`-style lists, so pointing a deployment at an *additional* private bundle directory —
the seam these defaults must not close — works exactly as before.

`.env.example` documents the three as commented-out keys, so `cp .env.example .env` (the README
quickstart, which `tests/test_config.py` boots end to end) leaves the shipped defaults intact rather
than overriding them with a path that only resolves from one directory.

## Code and data separated

`templates/` and `evals/` mixed code with the declarations the code reads, and `eln/` held sample
exports. The code moved into `src/`; the data stayed at the repository root under its existing
config default (`templates/`, `evals/cases`, `evals/retrieval_corpus`), and `eln/exports` became
`data/eln-exports` so no root directory remains holding only somebody else's samples. Data resolved
through `Path(__file__).parent` — `science/bo/benchmarks/data`, `science/safety/rules.yaml` — moved
with its code, because it belongs to the module rather than to a deployment.

Connector bundles keep manifest, code and skills colocated: that is D-109/D-118's design, and it is
what makes adding a capability a directory rather than an edit.

## Verification

`make lint type cov` green (1551 passed, 83% over the shipped tree — 206 modules measured, which is
every first-party module). All eight declaration validators pass, which is what proves the
`module:callable` strings in `connector.yaml` and `datasource.yaml` were repointed, since mypy
cannot see those. Every component `deploy/entrypoint.sh` dispatches imports. `git mv` throughout, so
`git log --follow` still reaches each file's history; `.git-blame-ignore-revs` lists the three
restructure commits so `git blame` skips past them to real authorship.
