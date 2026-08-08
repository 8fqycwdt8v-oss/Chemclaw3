# D-2026-08-08-a-prefix-the-documents-never-carried — the string a gate matches on belongs to one definition, and the prose that teaches it is checked

**Status:** accepted

## Context

`D-2026-08-07-a-manifest-must-say-who-may-read-it` namespaced group-derived entitlements: a token's
`groups` claim no longer joins the role set flat, but as `group:<claim value>`, so a directory group
named like an app role cannot silently pass a write-tool gate. That decision was implemented
correctly and is enforced in one place — `api/auth.py` — and the operator guide,
`docs/guides/sharedrive-concept.md`, documents the prefix and even calls it out in bold.

**The documents an operator reads first were never updated.** Three of them still described a
group-gated share as naming the group's *object-id*:

- `src/chemclaw/ingest/sources/sharedrive/datasource.yaml` — "emit the `groups` claim and name the
  group's object-id here". This is the shipped manifest, and copying it is the documented way to
  attach a real share.
- `src/chemclaw/ingest/documents/README.md` — "or as a group object-id under
  `CHEMCLAW_ENTRA_GROUP_CLAIMS_AS_ROLES`".
- `src/chemclaw/ingest/documents/retriever.py` — "group object-ids too".

`core/config/entra.py` had the same gap on the flag itself, and `binding.py`'s **refusal message** —
the one an operator actually hits, since a manifest that omits `required_roles` is rejected at load
— told them to set it to "the Entra app role or AD group object-id that gates it".

An operator following any of these writes `required_roles: [<guid>]`. The turn's roles carry
`group:<guid>`. The sets do not intersect, so `_entitled()` returns False, and the retriever's
contract is to decline by **returning no evidence** — deliberately, so a database blip cannot fail a
turn. The result is a correctly mounted, correctly crawled, fully indexed corpus that answers
nothing, with no exception, no log line above debug, and no failing validator. `make
datasource-validate` passes: the binding is valid, it simply gates on a value nothing will ever
present.

This is the same failure mode D-2026-08-07 was written to prevent, one layer out. That ADR's own
argument was that a security model whose failure is silent is not a security model; the fix it
shipped is silent when *configured* from the documents shipped beside it.

Two causes, and the second is the one worth fixing:

1. The prose was not updated with the code. Ordinary.
2. **The string was a hand-typed copy in every one of those places.** `GROUP_ROLE_PREFIX` existed as
   a constant, in `api/auth.py`, and no document could cite it: `chemclaw.ingest` may not import
   `chemclaw.api` (`tests/test_layering.py`), so `binding.py` could only have re-typed `"group:"`.
   Five copies of one security-relevant string with nothing holding them together is
   `D-2026-08-05-one-rule-in-three-places-is-three-rules`, and it went the way that ADR predicts.

## Decision

### 1. The prefix moves to the kernel that owns the role vocabulary

`GROUP_ROLE_PREFIX` now lives in `chemclaw.core.identity_context`, beside `get_current_roles` —
the module every gate already reads the roles *from*. `api/auth.py` imports it under its own name,
so `auth.GROUP_ROLE_PREFIX` keeps resolving for existing readers.

It is not an HTTP concern. `api/auth.py` is merely where the claim is stamped; the string is part of
what a role *is*, which is why the manifests, the refusal message and the guides all have to name
it. Placed in `core`, `ingest` can cite the definition instead of re-typing it, with no layering
exception — `chemclaw.ingest → chemclaw.core` is an existing declared edge.

### 2. The refusal message interpolates the constant

`DocumentShareBinding`'s "a share must say who may read it" error now names
`` `group:<claim value>` `` — built from `GROUP_ROLE_PREFIX`, not typed — and says in the same
breath that the bare object-id matches nothing. That message is the highest-traffic documentation
this feature has, because it is the one an operator reads at the moment they are writing the field.

### 3. The prose that teaches a gate is a claim, and it is checked

`test_every_place_that_teaches_a_group_gate_names_the_real_prefix` reads the six documents that tell
an operator how to write a group entitlement — the shipped manifest, the package README, the
retriever docstring, the binding, the Entra config section and the operator guide — and asserts each
contains `GROUP_ROLE_PREFIX`. It fails if any of them stops naming it, which is verified by
mutation rather than assumed: replacing `group:` in the manifest fails the test.

This is `D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check` applied to a security string
rather than a path. The check is deliberately weak — "contains the prefix", not "explains it
correctly" — because a weak check that runs is worth more than a strong one nobody can write, and
the failure it must catch is *silence about the prefix*, which is exactly what containment detects.

A second test, `test_a_group_gated_share_answers_for_the_prefixed_claim_and_not_the_bare_one`, pins
the behaviour itself: with the claim in the turn's roles, the prefixed binding is entitled and the
bare one is not. The documents can now only drift from the code by failing a test.

## Consequences

- A group-gated share configured from any shipped document works. Before this, only an operator who
  had read the guide got it right.
- One definition of the prefix, cited rather than copied. Changing it is one edit and a test run.
- The check is containment, so a document could still name the prefix and explain it badly. That is
  the residual, and it is stated rather than papered over: no test reads for meaning.
- Nothing about the enforcement changed. `api/auth.py` prefixed group claims before this ADR and
  prefixes them after; every existing deployment's behaviour is identical. This is a correction to
  what the system *says about itself*, which is why it needed no migration and no version gate.

## Alternatives rejected

**Fix the prose and stop there.** It is the whole visible defect and about ten minutes of work. It
also leaves five hand-typed copies of a security string with nothing holding them together, which is
the mechanism that produced the defect — the next change to the prefix reintroduces it, in exactly
the documents nobody thinks to grep.

**Accept the bare object-id in the gate too.** Matching both the prefixed and unprefixed forms would
make every wrong manifest work. It also un-does D-2026-08-07: the unprefixed value is precisely what
lets a directory group named `process-chemist` satisfy the app role of that name, and the gate this
would loosen is shared with every write tool and skill. The prefix is load-bearing.

**Put the constant in `chemclaw.ingest.documents`.** It is where the confusing document lives, but
it is the wrong owner: `api/auth.py` writes the value and would then import *up* into a sibling
package, which is the layering violation this repository's kernel rule exists to prevent.
