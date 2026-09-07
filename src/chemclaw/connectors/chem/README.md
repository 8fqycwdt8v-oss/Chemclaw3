# `connectors/chem` — a declaration this release does not run

A manifest and nothing else: **an `endpoint:` with no `server/`.** The capability — structure
rendering, compound resolution, stoichiometry — is `Chemclaw3-mcp`'s, served from its own pod, and
what stays here is the `connector.yaml` that four validators resolve tool names through plus the
chart's `connectors.chem.url` saying where to dial it (D-2026-08-09).

That is the whole bundle, and the shape is deliberate: a capability moving out of this repository
does not take its declaration with it, because the declaration is how core knows the tool exists,
what it is called, and which entitlement reaches it.
