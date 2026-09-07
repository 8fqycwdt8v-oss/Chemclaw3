# D-2026-09-07-a-borrowed-helper-is-declared-because-copying-it-drifts — `_KNOWN_PRIVATE_IMPORTS` holds a row again, and a third grant bucket exists

**Status:** accepted · **Date:** 2026-09-07 · **Supersedes** the consequence
`D-2026-08-08-a-private-import-of-a-type-alias-is-not-a-dependency` ends on
("`_KNOWN_PRIVATE_IMPORTS` is empty and kept"). Everything else in that ADR stands, including the
rule the row is granted *under*.

## Context

D-2026-08-08 removed two private-module imports rather than declaring them, and closed by recording
that the allowlist they had lived in was empty. The guard's own comment reinforced it: *"It was
empty for a while, **and that emptiness was the point**: the two rows that lived here were removed
rather than re-blessed."*

That is no longer true, and it stopped being true in the last commit on `main` before this one.
The wave-7 token-accounting fix made `agent/turn_usage.py` import
`langchain_openai.chat_models.base._create_usage_metadata`, which tripped **both** provider guards —
`tests/test_third_party_layering.py`'s private-import ratchet and `tests/test_llm_provider.py`'s
declaration of every module that may name a provider distribution at all. Both were right to fire.
The import was kept and declared, and a *third* grant category was added beside `_CLIENT_SEAMS` and
`_TYPES_ONLY` to hold it.

**The argument for all of that exists — in a commit message and two test comments.** It is not in
`docs/decisions/`. So a session reading D-2026-08-08 today finds an ADR whose stated consequence is
false, with no successor saying so, which is precisely the state
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` and
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` were written about, one level up. This
ADR is the missing record.

## Decision

**One private symbol may be imported from the provider SDK, declared in both guards with its
reason, and asserted by driving the real function.**

`agent/turn_usage.py` imports `_create_usage_metadata` from `langchain_openai.chat_models.base`.
It is declared twice, because two different rules cover it:

- `tests/test_third_party_layering.py::_KNOWN_PRIVATE_IMPORTS` — keyed by `(file, target)`, the
  rule against importing any dependency's private module. That allowlist is no longer empty.
- `tests/test_llm_provider.py::_HELPERS_ONLY` — a **third bucket** in the provider seam, beside
  `_CLIENT_SEAMS` (the two modules that may build a client) and `_TYPES_ONLY` (the mock gateway,
  which may name the wire format). Every module naming a provider distribution is in exactly one of
  the three, or `test_a_provider_client_class_is_imported_only_at_the_two_declared_seams` fails.

### Why re-implementing it was the worse option

The import exists because of a hole with a measurement behind it, not a convenience. With
`method="json_schema"` the SDK parses inside `_agenerate`, so a judge reply that fails validation
raises **before** `on_llm_end` fires and the call books nothing — measured, **1,100 tokens booked of
a served 6,600**, on the verifier's own documented degrade path. The raw HTTP body arrives on
`on_llm_error` instead, and `_create_usage_metadata` is the function that turns a provider's usage
block into the shape the rest of this system counts in.

Writing our own copy of that normalisation is a copy of a shape upstream owns, in a module that
would then drift in silence — including the cache-token *detail* keys, which a `service_tier`
response prefixes and which `turn_usage.graph_usage_tokens` subtracts to get the priced residual.
A drifted copy books every cached token as full-price input and leaves `turn_costs` reporting that
a deployment which caches on every turn has never read a cache. That failure is silent in exactly
the way a token ledger must not be.

### Why the grant is a third bucket and not a widened second one

`_TYPES_ONLY`'s safety comes from an assertion it can make: the target is a `.types` module, so the
grant cannot smuggle a client. Admitting a helper would have retired that check — a helper lives
*beside* the client class it belongs to (`_create_usage_metadata` and `ChatOpenAI` are both in
`chat_models.base`), so the module path says nothing about which was imported.

`_HELPERS_ONLY` therefore asserts by **symbol**, not by module: the imported name, with leading
underscores stripped, must begin lower-case, i.e. name a function rather than a class
(`test_a_helper_only_module_holds_no_client`). The commit that wrote it records that its first
version read the module path, compared `"base"`, passed, and would have accepted `ChatOpenAI`
happily; the second failed on the leading underscore of the name it exists to permit. The shipped
version was driven both ways — green as written, red with a client added.

## Consequences

- `D-2026-08-08`'s closing consequence is superseded. Its *rule* is not: a private import is
  declared with its reason or removed, and the ratchet is what forces the choice. The row here was
  argued for, not drifted into, which is the distinction that ADR's emptiness stood for.
- The blast radius is one function on one module. A `langchain_openai` bump that renames or moves
  `_create_usage_metadata` is an `ImportError` at process start of the front door and the worker —
  the reason the rule exists — and that is why it is asserted rather than merely declared.
- A future author who finds this row stale deletes it: `test_no_declared_private_import_is_stale`
  fails on a row whose import has gone, so the allowlist cannot outlive its subject.

## What keeps it true

- `tests/test_upstream_surface.py::test_the_gateway_client_still_publishes_cache_tokens_under_the_two_flat_keys`
  drives the real `_create_usage_metadata` and asserts the two flat cache keys `turn_usage` reads,
  **and** the absence of the per-TTL pair — so a rename, a moved symbol or a changed output shape
  turns red there rather than mis-billing in silence.
- `tests/test_llm_provider.py::test_a_provider_client_class_is_imported_only_at_the_two_declared_seams`
  fails if any module gains or loses a provider-SDK import without a row in one of the three
  buckets.
- `tests/test_llm_provider.py::test_a_helper_only_module_holds_no_client` fails if the granted
  module imports a class from that distribution.
- `tests/test_third_party_layering.py::test_no_declared_private_import_is_stale` deletes the row for
  us when the import goes away.

## Alternatives rejected

**Re-implement the normalisation in `turn_usage.py`.** A silently-drifting copy of a shape upstream
owns, over a value nothing downstream can sanity-check. Rejected on the cache-detail keys alone.

**Drop the `on_llm_error` accounting instead.** That is the hole the wave-7 fix closed: it books
1,100 of 6,600 served tokens on a path the verifier documents as normal. Leaving it open makes the
spend cap and `turn_costs` wrong in the direction that under-reports.

**Widen `_TYPES_ONLY`.** Retires the one assertion that makes that bucket safe to grant, in order to
avoid writing a second dict. Rejected.
