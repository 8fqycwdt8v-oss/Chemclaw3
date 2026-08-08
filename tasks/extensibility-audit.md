# Extensibility audit: what does it actually cost to add a thing?

**Date**: 2026-08-08 · **Scope**: this repository · **Method**: measured, not read · **Status**: report only, no code changed.

The question was whether every part of the system that changes on a regular cadence — a new tool, a
new agent, a new data source, a new skill, configuration, user management, routine operations — is
arranged so that adding or changing one is cheap. Prose about a seam is evidence about its author's
belief, so every claim below is backed by a command that was run, and each is reproducible.

---

## The verdict in one paragraph

**The extension seams are real, and unusually good.** Five of them — connectors, data sources,
skills, profiles, templates — share one idiom (`PATH`-style discovery directory + a YAML manifest +
a config enable-token), and I confirmed by experiment that a new connector and a new data source can
be added from a directory *outside the repository* with **zero** edits to any file in it. The
hardcoding sweep found essentially no instance names leaked into core: seven connector names produce
six hits across 64,200 lines of `src/`, and every one is a comment, a docstring or an example
string. Configuration does not grow when you add things — every per-instance concern is a dict-typed
open map. Config↔`.env.example` parity is enforced bidirectionally by a test.

**Three gaps sit against that.** One is a live defect: a *second* instance of a parameterised source
silently collides with the first, and for a mounted document share the collision **deletes** the
first share's index. One is a seam leak: a connector bundle that wants to contribute a knowledge
note type must edit a `frozenset` in core, which is the one thing the connector seam promises you
never have to do. One is documentation: user and role management — the operation the question named
explicitly — is the only recurring operation with no runbook section at all.

---

## Surface-by-surface

| Surface | Discovery | Declaration | Validator | Runbook | Cost to add one | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **Connector** (tools + durable jobs + skills) | `connectors_dir` PATH-list | `connector.yaml` | `connector-validate` | §(iv) | 1 folder, 0 repo edits *(measured)* | **Trivial** |
| **Data source** (new instance of an existing kind) | `data_sources_dir` PATH-list | `datasource.yaml` | `datasource-validate` | §(iii) | 1 folder + 1 env name, 0 repo edits *(measured)* | **Trivial**, but see Finding 1 |
| **Data source** (a genuinely new *kind*) | same | same + a new engine package | same | §(iii) | 32–49 files *(measured, 2 commits)* | **Bounded** — the engine is the work, the seam is free |
| **Skill** | `skills_dir` PATH-list | `SKILL.md` frontmatter | `skill-validate` | §(i) | 1 folder, 0 repo edits *(measured)* | **Trivial** |
| **Agent profile** | `profiles_dir` PATH-list | one YAML | `template-validate` | §(iv-b) | 1 file | **Trivial** (1 shipped instance) |
| **Step template** | `templates_dir` PATH-list | one YAML | `template-validate` | §(iv-c) | 1 file | **Trivial** (1 shipped instance) |
| **Configuration setting** | `pydantic-settings` | `core/config/<domain>.py` | `test_config.py` (bidirectional) | — | 1 field + 1 `.env.example` line | **Trivial**, test-enforced |
| **Deployment / Helm value** | `.Values.config` free-form map | `values.yaml` | `helm-validate`, `test_helm_chart` | §(xiv) | 1 map entry; any setting, no chart edit | **Trivial** |
| **Process role** | `entrypoint.sh` wildcards | `CHEMCLAW_COMPONENT` | `test_helm_chart` | `deploy/README.md` | `connector-*` / `connector-worker-*` match by glob | **Trivial** |
| **User / role / entitlement** | Entra app roles + group claims | env: `tool_role_gates`, `skill_role_gates`, `entra_privileged_roles` | — | **none** | 0 code, but no documented procedure | **Undefined** — Finding 3 |
| **Knowledge note type** | — | `KNOWN_NOTE_TYPES` frozenset in `kg/note.py` | `kg-validate` | — | **1 core edit** + 1 dir | **Expensive relative to its neighbours** — Finding 2 |
| **Graph relation** | — | `KNOWN_RELATIONS` frozenset in `kg/relations.py` | `kg-validate` | — | **1 core edit** | same |

---

## What I measured, and how

### The seams hold — proven by adding things from outside the repo

A new connector, declared only in a scratch directory, reaches the live agent tool surface:

```bash
CHEMCLAW_CONNECTORS_DIR="$SCRATCH/extra-connectors:src/chemclaw/connectors" \
  python -c "from chemclaw.connectors.registry import discovered, connector_tool_names; ..."
# discovered: ['bo','calc','chem','molfp','pricing','qm','rxnfp','safety']
# tool names: [... 'quote_reagent' ...]
```

`pricing` is a bundle that exists nowhere in this repository. Its tool is offered. **Zero repo edits.**
The same held for a second JSON-ELN data source (`eln-json-site2`: discovered, enabled, adapter
built) and a new skill (`vendor-negotiation`: passes `skill-validate`, discovered by the agent).

### The leak sweep came back almost empty

Every connector name grepped across all of `src/`, excluding its own bundle and its `science/` engine:

| name | hits outside its bundle | what they are |
| --- | --- | --- |
| `chem`, `rxnfp` | 0 | — |
| `calc`, `bo`, `safety`, `molfp` | 1 each | a storm-test fixture, two docstring examples, a shipped-path default |
| `qm` | 2 | a docstring example, a comment |

Data-source names: the only non-comment hits are each half's *own* `name = "..."` class attribute.
No branch, no enum, no `if name ==`, no per-instance config field anywhere in core.

### Configuration does not grow with the system

- **336** settings fields; **0** are required — the system boots on defaults.
- **`.env.example` parity is enforced in both directions** (`test_env_example_documents_every_field`
  and `..._only_real_fields`), plus a test that `cp .env.example .env` actually boots.
- The shipped Helm chart sets **34** of them, and `.Values.config` is a free-form map, so any of the
  336 can be set without touching a template.
- **5** settings are dict-typed open maps — `connector_urls`, `tool_role_gates`, `skill_role_gates`,
  `retrieval_source_weights`, `model_routes`. These are exactly the per-instance concerns, and none
  of them needs a new field when an instance is added.

### The validators discriminate

Deliberately broken declarations, each confirmed to **fail**:

| Break | Result |
| --- | --- |
| data source naming a callable that doesn't exist | ✅ `brokensrc: retrieve: ... has no attribute 'NoSuchRetriever'` |
| data source with a mistyped config key | ✅ `badcfg: ... will not accept config ['bindingg']` |
| connector declaring a skill it doesn't ship | ✅ `declares skill 'no-such-skill' but no such skill exists` |
| connector with a dangling `params_model` | ⚠️ fails (exit 1) but as an **uncaught traceback**, not the clean report — `_job_problems` catches `ValueError`, and `resolve_params_model` raises `ConnectorJobError`. Cosmetic; CI still goes red. |
| **two shares colliding on one source name** | ❌ **`data source validation passed.`** — see Finding 1 |

---

## Finding 1 — A second data-source instance silently collides, and for a document share it deletes data

**Severity: high.** This is a live defect, not a design opinion.

`ingest/sources/registry.py:147` builds a half with `factory(**manifest.config)` — the manifest's
`name` is **never passed**. Three parameterised halves therefore fall back to a hardcoded default:

| half | fallback | file |
| --- | --- | --- |
| `ShareDocumentRetriever` | `"sharedrive"` | `ingest/documents/retriever.py:87` |
| `WarehouseVectorRetriever` | `"warehouse"` | `ingest/eln/warehouse/retriever.py:63` |
| `VendoredDatasetRetriever` | `"vendored"` | `ingest/sources/vendored_dataset.py:145` |

The retriever's own docstring says the opposite — *"name: The retriever id; the registry passes the
source's name."* It does not. Measured with two enabled shares:

```
enabled in config: sharedrive, sharedrive-eu
active_retrieve_sources() -> 'graph', 'sharedrive', 'sharedrive'   # both shares report one name
share_sources() keys      -> ['sharedrive']                        # two collapse to ONE
  sharedrive -> mount /mnt/sharedrive-eu                            # last one wins
```

So `/mnt/sharedrive` is **never crawled**, silently. And the sweep, run end to end against the
in-memory index with two real temp shares:

```
crawl A -> indexed=1 rows=1
crawl B -> indexed=1 rows=2
rows before sweep: ['Docs/alpha.md', 'Docs/beta.md']
sweep after B's drain -> removed=1
rows after sweep:  ['Docs/beta.md']          # share A's document is gone
```

With the same relative path in both shares (`Docs/report.md` — the migration's own example), B's row
simply overwrites A's and the row count never leaves 1.

This is precisely the failure `infra/sql/037` says its composite key prevents:

> *"Keyed by (source, path), not path alone: two mounted shares can hold the same relative path …
> and a global key would silently let the second share's crawl overwrite the first's row and then
> sweep it."*

The key is right. The value fed into it is not — both shares pass `source='sharedrive'`, so the
partition the schema built never partitions anything. The design anticipated this exactly and the
wiring lost it.

`datasource-validate` passes this configuration.

**Smallest fix**: pass the manifest name into the half. Because `GraphRetriever()` and friends take
no `name`, the clean version is either (a) `_build_half` passing `name=manifest.name` for factories
whose signature accepts it, or (b) making `name` a required argument of the parameterised halves and
having the registry always pass it, with the class-attribute halves left alone. (b) is the more
honest one — it deletes three fallback defaults that can only ever be wrong for the second instance.
Either way, add a `datasource-validate` check that two enabled sources cannot report the same name,
since that is the invariant the whole partition rests on.

**Note the asymmetry**: the *ingest* side is already correct. `active_ingest_source_names()` returns
manifest names and `sync_eln_entries(source=name)` keys cursors on them, so two ingest sources never
collide. Only the retrieve side drops the name.

---

## Finding 2 — A connector cannot contribute a note type without a core edit

**Severity: medium.** This is the one place the connector seam's central promise doesn't hold.

`KNOWN_NOTE_TYPES` (`kg/note.py:183`) and `KNOWN_RELATIONS` (`kg/relations.py:23`) are frozensets in
core. Two of the eleven note types are already contributed *by bundles*:

```
"job-result",    # connectors/qm/knowledge.py
"bo-candidate",  # connectors/bo/knowledge.py
```

So a new bundle whose job declares `publish_to_graph: true` and returns a note of a new type is a
folder-only addition right up until `kg-validate` rejects the note — at which point it needs an edit
to a file in `kg/`. Every other bundle contribution (tools, jobs, skills, profiles, queue, pods) is
declaration-only.

The closed vocabulary is *deliberate and correct* — the docstring's reasoning is sound (a typo makes
a note unfindable by every filter keyed on it, and the PR-gate is where a genuinely new type should
be seen by a human). The gap is that the vocabulary has no way to be *extended by declaration*. The
manifest already carries `skills:` and `profiles:` lists; a `note_types:`/`relations:` list, unioned
into the known sets at validation time, would keep the typo protection and the PR-gate visibility
while restoring "a bundle is a folder".

I could not measure this one from history: both recent note types landed inside squashed merge #97,
so no clean single-addition diff exists. The cost above is read off the code, not off a commit.

---

## Finding 3 — User and role management is the only recurring operation with no runbook section

**Severity: medium.** Nothing is broken; the knowledge is just not written down anywhere an operator
would look.

The mechanism is good: identity is entirely Entra, roles arrive as app roles or (with
`entra_group_claims_as_roles`) prefixed group claims, and every gate reads **one** flat role set —
`entra_privileged_roles`, `tool_role_gates`, `skill_role_gates`, a share binding's `required_roles`.
Adding a role, gating a new tool, or entitling a team is pure configuration. **Zero code.**

What's missing:

- `docs/guides/runbook.md` has fourteen numbered procedures — add a skill, add a data source, add a
  connector, add a profile, add a template, add a database, cut a release, restore a store — and
  **none** for identity. There is no "onboard a user", "grant a capability to a role", "revoke
  access", or "what app roles must exist in the tenant".
- **There is no inventory of the app roles a tenant must create.** Grepping the whole repository for
  role-shaped names returns exactly one: `chemclaw.sharedrive.reader`, and only because a manifest
  happens to use it as an example. Everything else is `""` by default. An operator standing this up
  has nothing to work from.
- **No leaver procedure.** `session_owners`, `session_messages`, `session_events`, `session_turns`
  and `user_preferences` all hold per-actor rows. Removing someone's Entra role stops new access;
  nothing removes or anonymises their data, and there is no command that does. In a system that
  advertises a GxP posture and documents PII handling in `SECURITY.md`, that is a question that will
  be asked.
- **No in-app kill switch.** Revocation is entirely "remove the app role and wait for token
  expiry". That may well be the right decision — but it is a decision, and it isn't recorded.

---

## Finding 4 — Four operational commands exist with no runbook entry

**Severity: low.**

| command | what it is | runbook mentions |
| --- | --- | --- |
| `make audit-verify` | verifies the tamper-evident hash chain over the GxP audit trail | **0** |
| `make share-sync` | crawls a mounted document share on demand | **0** |
| `make safety-validate` | force-compiles the safety rule tables | **0** |
| `make schedules-apply` | creates the Temporal Schedules | 0 — *but the Helm chart runs it as a Job, so this one is fine* |

`audit-verify` is the notable one: it is the check that makes the audit trail's tamper-evidence
meaningful, it is referenced by `values.yaml` and by the eval probes, it is not scheduled anywhere,
and no document tells anyone to run it or how often. §(xiii) covers what a restore does to the audit
trail without ever naming the command that checks it.

`share-sync` is the newest feature's manual entry point; §(iii) covers adding the source but not
running the first crawl.

---

## Finding 5 — `connector-validate` reports one class of error as a traceback

**Severity: cosmetic.** `_job_problems` (`cli/validate_connectors.py:143`) catches `ValueError`, but
`resolve_params_model` raises `ConnectorJobError`, which is not one. A bundle with a dangling
`params_model` exits 1 with a Python stack trace instead of the `connector validation failed: - …`
report every other failure produces. CI still goes red; only the author's experience suffers.

---

## Two structural observations, offered without a recommendation

**`NOTE_INDEX_SOURCES = frozenset({"vector", "lexical"})`** (`core/config/retrieval.py:142`) is the
one place core enumerates data-source names. Its docstring defends this on the grounds that both are
*shipped* names — which is true, and which also means the constant is wrong the moment a deployment
overrides one of those folders from an earlier `data_sources_dir` (a precedence the registry
explicitly supports). It is not a bug today. It is the only surviving instance of the pattern the
rest of the codebase eliminated.

**`profiles/` and `templates/` each ship exactly one instance.** Both are full seams — PATH-list
discovery, manifest, validator, runbook section — with one caller. By the repo's own Rule of Three
that would normally be an inline candidate; I mention it only because the audit's premise is
"is adding one easy", and for these two the honest answer is "presumably, but nobody has done it
twice yet". Adding a second of each is how you'd find out.

---

## What I could not measure

- **Note types and relations**: no clean single-addition commit exists (both landed inside squashed
  merge #97), so Finding 2's cost is read off the code rather than off history.
- **Connector additions**: same problem — all seven bundles arrived in the D-118 refactor. The
  zero-edit claim is proven by the live experiment above instead, which is the stronger evidence.
- **Anything needing a real tenant, broker or cluster**: live Entra token validation, federation and
  OBO exchanges, and a real `helm`/`kubeconform` render. These are the known open live edges
  (`docs/planning/BACKLOG.md`), not audit gaps.
- **The companion repos** (`Chemclaw3_ui`, `Chemclaw3_mock`) were out of scope, so I cannot say
  whether adding a connector or a data source here forces a matching edit there. That is worth
  checking — it is the most likely place a "zero-edit" seam turns out to cost something.

---

## Ranked

1. **Finding 1** — second data-source instance collides; a second document share deletes the first's
   index. A live defect with a demonstrated data-loss path, unguarded by its validator.
2. **Finding 3** — no identity/user-management runbook section and no app-role inventory. The one
   recurring operation with no written procedure.
3. **Finding 2** — a bundle contributing a note type needs a core edit; the connector seam's promise
   has one hole in it.
4. **Finding 4** — `audit-verify` and `share-sync` are undocumented operations.
5. **Finding 5** — one validator error path prints a traceback.

Everything else measured clean.
