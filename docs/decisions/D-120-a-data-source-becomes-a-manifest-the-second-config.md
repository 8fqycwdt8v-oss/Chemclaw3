# D-120 — A data source becomes a manifest: the second config-side union replaced by a folder

**Context.** The user's direction for this pass named data sources beside tools: *"For anything
which will be exchanged or added future on a regular basis (tools, datasources, etc) I want to have
nicely defined generic connector approaches. Keep in mind that the number of databases, tools and
of course user will increase in future significantly."* D-118 did the tool half. This is the other.

`sources/` already had the right *contract* — `DataSource` with two independent, optional halves
(D-054), which nothing here changes. What it did not have was a way to attach one without editing
core. Adding a source meant an entry in `DATA_SOURCES` (a dict of factories in
`sources/registry.py`); adding a source that carried its own config meant three edits: a pydantic
model in `chemclaw/config.py`, an arm of the `DataSourceSpec` discriminated union, and a branch in
`build_data_source`. That was D-076, and it was a reasonable answer to the question asked at the
time — "how does a source carry per-instance config?" — but it makes the cost of a source scale
with core, which is the wrong direction for the thing the user says will grow most.

**The defect that decided the shape.** A dict of factories cannot say that a source *has* an ingest
half without also naming what builds it, so every adapter was imported at the registry's module
scope. The two consumers want disjoint halves — `gather_evidence` wants retrievers in the chat
process, the durable ELN sync wants adapters in a worker — and each was paying for the other.
Measured from a clean interpreter, `active_ingest_source_names()`, which returns two strings:

```
BEFORE  names=['eln-json']  modules=836  heavy=[drfp, numpy, psycopg, rdkit]
AFTER   names=['eln-json']  modules=292  heavy=NONE
```

The ELN sync worker was loading `report.retrievers` — rdkit, the reaction-fingerprint index, the
Postgres note index — to learn two names. Nothing failed; the cost is image size, process memory
and start-up time, and it grows per source added. This is the same class of defect as D-118's
`params_model` finding, arriving through a different mechanism: there a string in YAML resolved an
import no reader could see, here the registry's *shape* forced one.

**Decision.** A data source is a folder with a `datasource.yaml`, discovered from a search path,
enabled by name — structurally identical to a connector bundle.

- `sources/manifest.py::DataSourceManifest` (`extra="forbid"`): `name`, `description`, optional
  `ingest`/`retrieve` as `module:callable`, and free-form `config` passed as kwargs.
- `sources/registry.py` discovers manifests over `data_sources_dir` (OS-pathsep, earlier wins) and
  resolves a half **only when that half is about to be built**. `discovered()` is cached; built
  halves never are, so per-call config still applies.
- `DATA_SOURCES`, `build_data_source`, `DataSourceSpec`, `JsonElnSourceSpec`, `OrdElnSourceSpec`
  and `data_source_specs` are deleted. No back-compat shim: the user's direction for this pass was
  that breaking changes are acceptable.
- `make datasource-validate` (`scripts/validate_datasources.py`) resolves every declared half and
  binds its `config` against the real signature. This seam was the only registry with no validator
  — defensible when a source was Python that `mypy` checked, not once it is a string in YAML.

**Why `config` is free-form rather than typed.** It is the one thing D-076 gave that this takes
away, so it is worth being explicit. A typed union validates config at config-load; the cost is
that every adapter needs a parallel pydantic model in core, kept in step by hand. Here the
callable's signature *is* the schema — there is nothing to keep in step — and the validator binds
against it in CI, which catches the same typos at the same point in the workflow. The one case it
does not catch is a `config` value of the wrong *type* for a parameter with no annotation; adapters
in this repo are annotated, and `mypy --strict` covers them.

**Consequences.**

- Attaching a source touches zero core Python. `tests/test_datasource_seam.py` is the acceptance
  test and demonstrates it: it attaches a working source by writing one YAML file into a tmp dir.
  It previously had to `monkeypatch.setitem` a dict inside `sources.registry` — a test reaching
  into core to add a source is evidence the seam does not work.
- A second instance of an existing adapter (a staging ELN drop) needs no code at all, and a
  deployment can override a shipped source by mounting a directory earlier on the search path.
- `tests/test_datasource_isolation.py` holds the import property in a subprocess, counterfactually
  verified: restoring one module-level adapter import fails it.
- `chemclaw/config.py` now has **no** pydantic models. `McpServerSpec` went to a connector manifest
  in D-118 and `DataSourceSpec` goes to a source manifest here, and they went the same way for the
  same reason: each described the internals of one attached thing. The rule that leaves behind is
  recorded in that module's docstring — *config says which and where; a manifest says what* — and
  it is the rule that keeps the config file from growing with the deployment.
- Supersedes D-076. `sources/base.py` (D-054) is untouched; the contract was never the problem.

**Not done, deliberately.** `report.retrievers` still loads in the chat process, because the one
active retrieve source genuinely needs it — the win there is structural (a future ingest-only
driver no longer lands in the chat pod), not a number today, and claiming otherwise would be
dishonest. The Snowflake ELN source remains deferred; it is now a manifest and an adapter class,
with nothing owed by core.
