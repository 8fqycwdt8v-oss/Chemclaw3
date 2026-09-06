# D-2026-09-06-a-manifest-is-data-in-every-field-that-executes — the stdio rule, finished

## Status

Accepted.

## Context

This repository held two positions about the same class of file, and the 2026-09 wave-5 review put
them side by side.

**Position one, written down twice and enforced.** `connectors/registry.py` refuses
`transport: stdio` by default with the sentence *"a manifest is data"*
(`D-2026-08-26-an-empty-allow-list-is-not-an-allow-list`), because `command:` is run in the chat
process under the identity holding every connector token. `core/config/connectors.py` refuses an
unfunded wall-clock ceiling with *the same sentence*, because "a manifest in this repository still
cannot grant itself runtime the operator did not fund". Two merged decisions, one argument, one
shape: default off, an operator turns it on.

**Position two, in a field docstring.** `EntryBinding.where` is a raw SQL fragment interpolated
verbatim into a statement, and its validator says *"`where` is trusted by design"*, on the grounds
that it is "as trusted as the manifest itself; it is authored beside the `module:callable` that the
same file imports". That argument is coherent, and it is only coherent if a manifest is trusted
code.

Both cannot be right, and the second was winning by default, because the `module:callable` family
was ungated. Measured with a benign marker at module top level:

```
$ CHEMCLAW_CONNECTORS_DIR=… python -c "from chemclaw.connectors import registry; registry.job_tools()"
job_tools() built: ['evil']
MARKER_IMPORT_RAN exists: True
```

`job_tools()` is on the per-turn agent-build path, so that runs in the chat process on every turn,
before any authorization gate exists. `connector-validate`, `sink-validate` and
`datasource-validate` all exited 0. The sink and channel seams go further: the resolved callable is
*invoked*, with the manifest's own `config:` block as keyword arguments.

Five fields have this reach — `params_model`, `precondition` (connector), `ingest`, `retrieve`,
`commitments` (data source), `driver` (sink and channel) — plus `vector_store_provider` in settings,
which is not a manifest and is out of scope here.

## Decision

**A manifest is data, in every field that executes.** Position one is the repository's position; the
`where` docstring's premise is the one that was wrong, and this ADR does not change `where` — it
removes the premise `where` was leaning on. `where` remains trusted *as an operator-authored SQL
fragment in a file only an operator can place*, which is now a claim about who may write the file
rather than a claim that manifests are code.

**The control is a package allow-list, not a path check.** A `module:callable` may name `chemclaw`
plus whatever `CHEMCLAW_MANIFEST_DRIVER_PACKAGES` names — the same "an operator turns it on" shape
`connector_stdio_enabled` has, so this tree has one idiom for this rather than two. The check runs
in `core/manifest_io.check_driver_module`, before the import, because the import *is* the execution.

**What this defends and what it does not.** The threat is a manifest arriving on a discovery path
that is not the installed package — a mounted ConfigMap, a CI job syncing a sibling repo — because
discovery is enablement. Such a directory is not on `sys.path`, so a module it names has to be
importable already, and the allow-list is exactly the set of things that are. It does **not** defend
against someone who can write a `.py` inside the installed `chemclaw` package: that is the code, and
no manifest rule stops it. The wave-5 finding is right that all four default discovery roots sit
inside the importable package and a manifest-only folder there is a PEP 420 namespace package —
which is a reason to be precise about the claim, not a reason to make a different one.

**The D-118/D-120 property wins where it conflicts.** Adding a bundle is still zero core edits: a
driver living in this tree needs nothing at all, and a driver outside it is one env var set once by
the operator who mounted the directory the manifest arrived in. Both arms are asserted, because a
gate with no working escape hatch is a deletion wearing a setting's clothes.

## Consequences

A deployment whose manifests name only `chemclaw` modules — every shipped bundle, sink, channel and
source — is unaffected: 8 connectors, 10 sources, 1 sink, 2 channels, 9 templates and 14 job tools
load unchanged. A site running a third-party driver must set one env var, and will find out at
startup rather than at first use, because these seams resolve at build time.

The three validators keep their exit codes and gain a refusal: `connector-validate`,
`sink-validate` and `datasource-validate` now fail on a driver outside the allow-list, which is what
the reviewer asked for — the gates that exist to check declarations now check the field that
executes.

Two things this ADR deliberately does not do. It does not parse `where`: that would be the "second
source of truth for a schema that already exists" `D-2026-08-04-the-schema-is-a-file` exists to
avoid, and the doctrinal conflict is settled without it. And it does not gate
`vector_store_provider`, which is a *setting* — an operator's own value, not a file that appears on
a mounted path — so it was never the same seam and folding it in would blur the distinction this
decision rests on.

## The robustness half, decided in the same commit

Four defects in the same six loaders were not doctrinal on any reading, and `core/manifest_io.py` is
where they are fixed once instead of six times.

- **An expansion budget.** A 375-byte `connector.yaml` cost 687 MB of RSS and 3.2 s; one level
  deeper it passed 9 GB. The bound has to be on the *expanded* size — `yaml.safe_load` shares
  aliased nodes, so the parse is 93 objects and a bound on it would have passed. `_expanded_size`
  memoises per node, so 43,046,721 nodes are counted in 93 steps of arithmetic. Refusal now costs
  221 MB peak and 0.0 s.
- **`RecursionError` is not a `YAMLError`.** 2000-deep nesting escaped every loader untranslated,
  naming no file and matching neither the `except ValueError` startup handlers nor Temporal's
  name-matched non-retryable set.
- **Duplicate keys.** PyYAML is silently last-wins and `extra="forbid"` cannot help, because the key
  is not extra. Measured, a manifest declaring `state_changing: [a, b]` and later repeating
  `state_changing: []` / `read_only: [a, b]` loaded with two write tools classified as reads — the
  plan gate's input (D-167) failing open from a copy-paste.
- **Unbounded prompt text.** `JobSpec.summary`/`description` and `Template.summary` had no maximum,
  and a 5.4 MB manifest produced a 5,200,664-character tool docstring. `MAX_SINGLE_TOOL_TOKENS`
  lives only in `tests/test_context_floor.py`, which measures the *shipped* bundles, so an
  out-of-tree bundle — the supported way to add a capability — was outside the ratchet CLAUDE.md's
  budget arithmetic rests on. `MAX_MANIFEST_TEXT_CHARS` is the bound such a bundle is held to.
  It bounds one *field*; it does not bound a bundle's total, which remains the ratchet's job.

Beside them: `DataSourceManifest.name` gains the `^[a-z][a-z0-9-]*$` its three sibling manifests
already require (defence in depth — the review could not turn a hostile name into an escape, and
metric labels are escaped at all four exposition sites); `publish/registry._load` wraps its
`ValidationError` so `sink-validate` reports a line rather than a traceback; `deliver.build` checks
the protocol `publish.build` already checked; and a bundle directory symlinked out of its discovery
root is no longer discovered — by *resolution*, not by refusing symlinks, because a Kubernetes
ConfigMap volume presents its own keys as symlinks and those land inside the mount.

One of the review's stated rationales is corrected rather than carried: the unwrapped
`ValidationError` did **not** burn a Temporal retry budget, because `"ValidationError"` is already
in `durable/publish._BAD_DATA_TYPES`. The fix stands on the reason `deliver/registry._load`'s own
docstring gives from wave 4 — a validator that catches the seam's error type should not have to also
catch pydantic's.

## What is left open

`agent/profile_discovery.py` has the same unbounded-prose hole in `AgentProfile.instructions` — a
500,000-character `data/profiles/p.yaml` loads and goes straight into the system prompt — and the
same bare `yaml.safe_load`. It is the sixth loader and it is not fixed here only because it was
outside this change's ownership; it is a `BACKLOG.md` row and the fix is two lines against this
module.
