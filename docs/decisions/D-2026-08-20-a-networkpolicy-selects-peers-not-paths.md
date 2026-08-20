# D-2026-08-20-a-networkpolicy-selects-peers-not-paths — the four bundles we host authenticate their own `/mcp`

**Status:** accepted · **Date:** 2026-08-20 · Carries out the `BACKLOG.md` §1 row of the same
name. Completes `D-2026-08-09-a-connector-we-do-not-run`'s credential rule by applying it to the
bundles this repository *does* run.

## Context

`bo`, `calc`, `molfp` and `rxnfp` shipped `auth: mode: none`. Their `/mcp` served every declared
tool — including `compute_xtb_energy` and the launchers for durable HPC and BO work — to anything
that could open a socket to the pod. `chem` and `safety` were already `mode: bearer`, but for a
reason that says nothing about ours: they are served by `Chemclaw3-mcp`, whose `connector_app`
enforces a credential, so `mode: none` there would not have meant "no auth needed" but "every call
is refused".

The control of record was the NetworkPolicy. A NetworkPolicy selects *peers*, not paths: it says
which pods may reach the connector, and every pod in the namespace could.

`connectors/server.py`'s `BearerAuthMiddleware` already existed, already failed closed on an
unresolvable manifest, and was already exercised. Nothing was missing but the declarations.

## Decision

**Each of the four declares `auth: mode: bearer` with its own `token_env`.**

`CHEMCLAW_<NAME>_MCP_TOKEN`, not `CHEMCLAW_<NAME>_TOKEN`, because for `calc` the shorter name is a
*different hop*: `CHEMCLAW_CALC_TOKEN` is the credential core presents to the remote calc server in
`Chemclaw3-mcp`. A name that means two hops is a name someone eventually points at the wrong one, so
all four take the longer form rather than three taking the short one and `calc` being the exception.

Both ends read the same variable per request — `BearerAuthMiddleware` on the serving pod,
`_EnvBearerAuth` on core and the workers — so a rotation takes effect without a restart, and
`chemclaw.env` mounting the whole secret map onto every Deployment means one value per bundle covers
both sides with nothing to keep in step.

**For local work the credentials are minted, never defaulted.** `chemclaw.cli.connectors_dev` fills
in a random token for any variable the environment does not already carry, and `--export-env` prints
them so a caller that starts core in a different process gets the same values. A constant would be a
credential committed to the tree, and the one thing worse than an unauthenticated dev server is an
authenticated one whose password is public — the second looks like a control. Minting is scoped to
bundles *we serve*: inventing a value for `chem` or `safety` would replace a clear
`MissingConnectorCredential` naming the unset variable with a 401 from a server that never heard of
the token.

`tests/test_connector_identity.py` asserts the property over the enabled set rather than over a list
of four names — the failure worth guarding against is the *fifth* bundle, and a list would pass the
day someone adds one with `mode: none`, which is exactly how the first four came to be that way.

## What turning it on exposed

**The probe allowlist did not survive being mounted.** `BearerAuthMiddleware` exempted `/healthz`
and `/metrics` by comparing `request.url.path`. Starlette does not strip a mount prefix from
`scope["path"]` — it records the prefix in `root_path` and leaves the path whole. Measured: a
`GET /molfp/healthz` reaches a middleware inside the mounted app as
`url.path == scope["path"] == "/molfp/healthz"`, `root_path == "/molfp"`.

In the cluster each connector is its own Deployment serving at the root, so the allowlist held
there. `chemclaw.cli.connectors_dev` mounts every bundle under `/<name>` — that is `make connectors`,
the live lane, and `tests/test_connector_transport.py` — and there it did not. With a credential
declared, the readiness probe `connectors.health` makes against `health_url` would have come back
401 and reported the whole fleet unreachable.

The bug predates this change by the whole life of the middleware and was invisible for a simple
reason: nothing was ever refused. Fixed by comparing the app-relative path; pinned by a test that
drives both the mounted and unmounted shapes, and mutation-proved by reverting it (one identity
test and four transport tests fail).

The same pass found the transport test rebuilding each bundle's endpoint from scratch, which
silently dropped the manifest's `auth` declaration — so it connected anonymously and proved nothing
about the credential the deployment requires. It now copies the real endpoint with the address
swapped.

## Consequences

- Four `values.yaml` `optionalKeys`, pinned by `tests/test_helm_chart.py`. `optional: true` for the
  upgrade reason its siblings carry, and with the same caveat: unset, the serving pod refuses every
  MCP request and core raises on the first call. That is the fail-closed direction, and still an
  operator's job to fill in.
- The tokens are deliberately **not** keys in `.env.example`: they are not `Settings` fields, and
  `Settings` is `extra="forbid"`, so a `cp .env.example .env` carrying them would abort every entry
  point at import. They are named in prose there instead, with that reason recorded.
- `infra/live/processes.sh` persists what it mints to `$RUN_DIR/connector-env.sh` and prints it with
  `processes.sh env`. Without that, every command run outside the shell that started the lane minted
  its own tokens and got 401s from healthy servers — a failure this change would otherwise have
  introduced.
