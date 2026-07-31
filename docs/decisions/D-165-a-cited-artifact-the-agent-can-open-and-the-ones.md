# D-165 — A cited artifact the agent can open, and the ones it should not try to read

## Status

Accepted. Implements W2.3 of the dataflow review's plan, closing the reachability half of D-158
that D-163 began.

## Context

D-124 built the artifact store to keep what a calculation produces besides its answer: the relaxed
geometry, the Hessian that cost minutes, the raw `vibspectrum`. It is content-addressed, deduped
across runs, eviction-managed, and finished — on the write side. Nothing could read it.

The read surface existed too. `PostgresArtifactStore.open` and `.list_for` have been implemented
and tested since D-124, and `Note.artifact_refs` validates a citation's *shape* against exactly
what `ArtifactRef.as_str()` writes. So the system could persist an artifact, could cite it in
merged knowledge, and could not open the thing it cited. D-163 sharpened this by a step: the agent
now gets calculation keys back from `find_calculations` and had no way to look behind any of them.

## Decision

**Two MCP tools on the `calc` connector, over the store's existing read methods**: `list_artifacts`
(a calculation key → what it produced) and `fetch_artifact` (an `<key>#<name>` reference → its
text). No new storage, no new backend, no protocol change — the seam was already there.

Two tools rather than one because `fetch_artifact` needs an exact name and nothing else could
supply it. The chain is `find_calculations` → `list_artifacts` → `fetch_artifact`, each step
handing the next its argument.

### A binary artifact is refused, not returned

`hessian.npy` and `dipole_derivatives.npy` are packed numeric arrays this system writes itself, and
`density.restart` / `orbitals.molden` are reserved for the DFT tier. None of them is reading
material — they exist so a *later calculation* need not redo the work — and none of them survives a
trip through a model's context in any useful form.

The alternative shapes are both worse. Base64 spends thousands of tokens on bytes nothing will
parse. An empty `text` with a `readable: False` flag beside it is a silent wrong answer waiting for
one unchecked field: an artifact that is present and fine gets reported as empty or missing. The
refusal names what the artifact is for, and cannot be misread.

### Readability is decided by decoding, not by a media-type table

`_MEDIA_TYPES` maps a producer's filename to a type and falls back to opaque bytes for one it does
not know. A rule built on it would therefore refuse perfectly readable output from any tool added
later, purely because its filename was not in a list written today — and the list is not the ground
truth anyway. Whether the bytes are text is a property of the bytes, so the code asks them:
`decode("utf-8")`, and a `UnicodeDecodeError` is the refusal. A `.npy` fails on its magic byte.

### The ceiling is what handles the Hessian

A Turbomole `hessian` is text, so it cannot be refused on type — and for a 76-atom molecule it is
single-digit megabytes of it. `calc_artifact_max_chars` (default 20 000) bounds every read;
`truncated` and the artifact's *full* `byte_size` come back with it, because a partial read that
does not announce itself gets quoted as the whole file. `max_chars` may ask for less and is clamped
to the ceiling, the same discipline `find_calculations`' `limit` follows: the argument is a request,
the cap is the deployment's.

### A missing artifact names what is stored instead

Artifacts are eviction-managed — the one thing in the calculation store that is, since D-011 forbids
evicting results — so a reference in an older note may genuinely point at reclaimed bytes. The error
lists what *is* stored under that calculation and says eviction is the likely reason, so "you asked
for the wrong name" and "it was reclaimed" are distinguishable without a second call.

## Consequences

- The by-products D-124 has been keeping become reachable, which is what makes keeping them worth
  anything. A note's `artifact_refs` is now a citation the agent can follow rather than a string it
  can only validate.
- `list_artifacts` returning empty is the common case and means "this run kept nothing", not "this
  calculation is missing" — the tool's docstring says so, because the model has no other way to
  tell the two apart and would otherwise report a present calculation as absent.
- Reading an artifact refreshes its access stamp through `PostgresArtifactStore.open`, so eviction
  scoring (least-valuable-first, by cost and idleness) now sees agent reads and not only
  calculator reuse. An artifact a chemist keeps coming back to survives longer, which is the policy
  D-124 intended and could not observe.
- `fetch_artifact` costs two round trips: the listing resolves the name to a content address, then
  the blob is opened. Addressing a blob directly by content hash would save one and would take the
  reference a note cites out of the tool's vocabulary, which is the only reason the tool exists.
- Both names are outside `_MUTATING_PREFIXES`, so `connector-validate` accepts them on the
  read/compute-only agent surface without an exemption.
