# D-2026-10-08-a-connector-name-has-one-owner — A connector's manifest comes from one place, and a name declared in two directories is a startup error

**Status:** accepted · **Date:** 2026-10-08

## Context

`Chemclaw3-mcp` publishes every `connector.yaml` it serves as the `chemclaw-contracts` package
(its `D-2026-10-08-the-fleet-publishes-its-contracts-as-a-pinned-git-package`), and this repository
takes the package as a dependency pinned to one commit. Eight of its bundles were manifest-only
copies of the fleet's, 29 to 126 lines apart, kept honest by a test that skipped without a sibling
checkout. They are gone; their `skills/` stay, because judgment is architecture layer 3.

Discovery was first-directory-wins, "like `PATH`", so an operator's directory could replace a shipped
manifest, and the loser was never parsed, never merged and never logged. That is how a copy and its
original could disagree unobserved, and with the copies deleted the only remaining use of it is the
Helm chart's documented override: mount a bundle with a shipped bundle's name. Nothing else in this
tree relies on it (searched: the registry tests, the chart and its values, the four-repo lane and the
kind lane; the one worked example, `pyexec`, is now shipped and was the only override the chart
documented).

## Options

1. **Keep first-wins.** Costs nothing and keeps the override. Costs the silent disagreement: which
   of two manifests describes a capability depends on the order of a path list, and the loser is
   invisible. With the fleet's manifests on the default path, an operator's stale copy would shadow
   a fleet change with no signal.
2. **First-wins with a warning.** Visible, still order-dependent, and a warning at import in every
   pod is the kind nobody reads until the day it matters.
3. **A name declared in two directories is a startup error naming both files, with no escape.**
   One owner per name. An operator who wants a shipped connector somewhere else has
   `CHEMCLAW_CONNECTOR_URLS` (the chart's `connectors.<name>.url`), which moves the endpoint without
   replacing the manifest; one who wants a different tool surface drops the shipped directory from
   `CHEMCLAW_CONNECTORS_DIR` (and names the rest again).
4. **An error, with an opt-in flag that restores first-wins.** Keeps the override for the one site
   that wants it. It is a setting nobody has asked for, it carries the original defect behind a
   switch, and the chart's override was the only user.

## Decision

**Option 3.** The default `connectors_dir` is the installed package's `manifests/` followed by this
image's own bundles; a name in both is refused at discovery. A directory with no `connector.yaml`
still contributes its `skills/` and `profiles/` to the connector of its name, which is how the
judgment kept here loads beside a manifest the fleet owns; `make connector-validate` refuses a
`skills/` directory that names no discovered connector. A manifest's `contract_version` is compared
with the server's `/healthz` when a session opens: a different major refuses that connector by name
through `capability_degraded`, a different minor is logged, a missing value on either side is
unknown and never refuses.

## Consequences

A deployment that mounted a bundle over a shipped one (the chart's old `pyexec` example) now fails
at boot and must use `connectors.<name>.url`. Pinning is by commit until the fleet's tag can be
pushed; `uv.lock` records the resolved commit either way. The image build needs `github.com`
(`uv sync --frozen` with `git` installed in the image, which it already was).

Revisit when: a deployment shows a need to replace a fleet manifest that `CHEMCLAW_CONNECTOR_URLS`
and a narrowed `CHEMCLAW_CONNECTORS_DIR` cannot meet.

Held by, for the name-owner rule: `tests/test_connector_registry.py::test_a_name_declared_in_two_directories_is_refused_naming_both`,
`::test_a_connectors_judgment_is_found_beside_a_manifest_it_does_not_hold`,
`tests/test_sibling_manifest_agreement.py` (the installed package is the only source of the
fleet's manifests, and `pyexec` and the process bundles stay off by default),
`tests/test_deploy_chart.py::test_mounting_a_bundle_the_image_already_ships_is_refused_at_render`
and `::test_the_image_bundle_names_are_the_names_the_image_declares`; for the contract-version
handshake: `tests/test_connector_contract_version.py` (all three outcomes through a stub server, the
answer remembered per window, a refusal that does not trip the reachability breaker, a flaky
`/healthz` that cannot flip a verdict).
