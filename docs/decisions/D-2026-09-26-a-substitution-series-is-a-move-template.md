# D-2026-09-26-a-substitution-series-is-a-move-template — the substitution-series template ranks the drawn molecule's own regioisomers

**Status:** accepted · **Date:** 2026-09-26 · **Decided by the owner, 2026-09-26.** Closes the
`BACKLOG.md` section *"No substitution-product enumerator, so one class of "which molecule" question
stays a proposal"* (issue #464), opened by
`D-2026-09-20-the-chain-already-existed-and-it-is-called-a-template`.

## Context

`Chemclaw3-mcp` PR #129 adds `chem.enumerate_substitutions(smiles, substituent=None,
mode="move"|"add")`, read-only and admission-gated in the server. Its `move` mode returns the input
and its positional isomers; `add` returns the products of one new substitution and needs
`substituent`.

## Decision

Core declares the tool in `connectors/chem/connector.yaml` (`tools` and `read_only`) and ships
`data/templates/substitution-series.yaml`: `enumerate_substitutions` → `rank_species` with
`ranking: custom` and the tool's labels, then a report that states a stability order is not a
regioselectivity. Only `smiles` is required, so the template is dispatchable by a hypothesis check
(`tests/test_hypothesis_dispatch.py::_DISPATCHABLE_TEMPLATES`).

**`mode` is fixed at `move`.** `add` needs `substituent` as a second required input, which would
take the template out of the dispatchable set; and `mode` cannot be an optional input, because an
unset input resolves to `None` and a tool step passes it through to a `move`/`add` literal that
refuses it. The `add` question stays a direct tool call followed by `rank_species`.

**Level is the job's default**, as `stereoisomer-ranking` uses, because nothing measured here says a
substitution series needs a conformer search per isomer the way `tautomer-resolution`'s
acetylacetone case did; the report step names `thorough` where an ortho isomer can hydrogen-bond.

## Consequences

The launcher's text was trimmed to keep `tests/test_context_floor.py`'s ceiling unchanged, which
leaves little headroom for the next launcher. Declared here before the sibling PR merges: until a
`chem` server answering the tool is deployed, the template's first step fails at run time with the
server's own unknown-tool error.

Revisit when: a template can take an optional input that is omitted from a tool call when unset,
at which point `mode` (and the `add` question) can be exposed without a second required input.
