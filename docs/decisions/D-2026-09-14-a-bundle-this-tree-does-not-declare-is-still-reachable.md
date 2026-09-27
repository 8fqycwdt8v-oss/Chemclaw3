# D-2026-09-14-a-bundle-this-tree-does-not-declare-is-still-reachable — `pyexec` needs an operator's values file, not a stub in `src/`, and the row saying otherwise was stale in four places

## Status

Accepted. Supersedes the `docs/planning/BACKLOG.md` row *"`pyexec` is merged in the fleet and
unreachable from any deployment here"* on its headline claim, and keeps the narrower claim inside it.

## Context

The 2026-09-13 capability audit scheduled "wire `pyexec` behind its enablement ADR" as Wave 1 work,
on the strength of that row. The row opens by asserting that `grep -rn pyexec` in this tree finds
only one ADR and a scratch file, that `values.yaml` has no `connectors:` entry for it, and that the
change owes an `egressPorts` entry for 8899.

**Measured, three of those four are false today**, superseded by
`D-2026-09-07-a-seam-that-stops-at-the-chart-is-not-a-seam` and by the live lane:

- `pyexec` is named in `deploy/helm/chemclaw/values.yaml`'s `extraConnectors` example, in
  `tests/test_deploy_chart.py`, three times in `tests/test_context_floor.py`, and five times in
  `infra/live/e2e-full-stack/up.sh`.
- The missing `connectors:` entry is no longer what makes it unreachable — an operator's own values
  file supplies it, exactly as it does for a bundle the chart does not know.
- `networkPolicy.egressPorts` stopped hand-naming its keys in that same ADR; the template now
  ranges the map, and `tests/test_deploy_chart.py` renders an operator-invented key and asserts the
  port appears. `pyexec: 8899` differs from the tested `props: 8850` in no field the template reads.

What is still true is the part of the row about a **stub in this tree**, and it is why nothing is
being added under `src/chemclaw/connectors/pyexec/`.

## Decision

**`pyexec` is reachable by a deployment today, with no code change here, and it stays that way: no
manifest stub ships in this tree.**

A release enables it in one values file, adjacent stanzas, all four map- or list-valued and all
already consumed generically: `extraConnectors.bundles` (the mounted ConfigMap),
`connectors.pyexec` with its `url:` (which is also what turns it on), `networkPolicy.egressPorts`
carrying 8899, and the bearer in `secrets.optionalKeys`. `deploy/README.md` already writes the
recipe out with `props` as the worked example.

**Why not the stub, restated because it is the whole decision.** `connectors_enabled` empty means
*every discovered bundle*, and the shipped default is empty — so a manifest under
`src/chemclaw/connectors/pyexec/` puts arbitrary LLM-authored Python execution on the agent surface
of every fresh checkout, by default, and `tests/test_probe_coverage.py` would demand a `run_python`
probe while the context floor took another ~1,142 tokens for a tool nearly no deployment binds.
Turning a code-execution tool on by default is a decision; the seam means it is not a *necessary*
one, because the capability is reachable without it.

**Two properties make the operator path an act rather than an accident**, and both are checked
rather than argued. A mount alone enables nothing: the bundle is discovered and, without
`connectors.pyexec.enabled: true`, not enabled. An entry alone starts nothing: `registry.enabled()`
raises `ConnectorError` naming the unknown bundle, in every pod. And the chart *refuses to render*
a release that enables no bundle, precisely so `CHEMCLAW_CONNECTORS_ENABLED` is never the empty
string in a deployment — so the "empty loads everything" semantics the row named as the branch point
cannot arise on this path at all, and does not need changing.

## Consequences

- **The Wave 1 item is smaller than it was scheduled as, and what it actually contained was a bug.**
  `infra/live/e2e-full-stack/up.sh` gave the `pyexec` *server* its token and never gave the *front
  door* one — `CHEMCLAW_PYEXEC_TOKEN` was absent from the export block five siblings sit in. That is
  the exact failure the comment four lines above that block warns about: `/healthz` is
  unauthenticated, so the connector reports healthy while every `/mcp` call it makes is rejected and
  the turn degrades with no clue why. Fixed here.
- **A deployment that mounts `pyexec` gets `run_python` with no probe covering it.** That is a real
  residual and it is stated rather than closed: `tests/test_probe_coverage.py` reads the bundles on
  *this* checkout's `connectors_dir`, so a bundle declared only in `Chemclaw3-mcp` is outside it in
  both directions. Closing it means a probe corpus that can name a tool this repository does not
  serve, which is the same cross-repository problem `SERVED_ELSEWHERE` and
  `tests/test_sibling_manifest_agreement.py` already carry, and it belongs with them rather than
  here.
- **The cost stays on `FLEET_PUBLISHED_ALLOWANCE`, not on `PREFIX_BOUND`.** `pyexec` is priced at
  1,142 tokens over 1 tool under the looser bound, which is right while no chart entry and no
  `enabled()` here returns it; a stub would mechanically force it into `SERVED_ELSEWHERE` and raise
  both compaction defaults for every deployment on account of a bundle those deployments do not
  bind.
- **Reopening the stub is a new decision** with a named subject: it needs the probe above, a
  re-measured floor, and an argument for a code-execution tool being on unless a deployment says
  otherwise. `D-2026-08-25-a-sandbox-is-a-server-not-a-verb`'s "what this is not" paragraph still
  bounds what may be added without one.

## What keeps it true

- `tests/test_deploy_chart.py` — the rendered NetworkPolicy carries an operator-invented
  `egressPorts` key, and an `extraConnectors.bundles` entry mounts on every pod that resolves the
  registry and sets `CHEMCLAW_CONNECTORS_DIR`.
- `tests/test_helm_chart.py` — `secrets.optionalKeys` is the shipped set; an operator's addition is
  outside it by construction, which is what makes the bearer's variable name the *manifest's* to
  declare rather than this chart's to write down.
- `src/chemclaw/connectors/registry.py` — a named-but-undiscovered bundle raises rather than being
  skipped, so half the operator recipe fails loudly instead of serving nothing.
- `tests/test_context_floor.py::test_the_bundles_both_repositories_declare_are_the_ones_charged_to_the_allowance`
  — a stub added here would move `pyexec` into `SERVED_ELSEWHERE` mechanically, so the decision
  cannot be reversed quietly.
