# ADR-0001 — Runtime is Python

- Status: Accepted
- Date: 2026-07-19
- Mirrors: `DECISIONS.md` D-001

## Context

Chemclaw spans conversation orchestration (MAF), durable job execution
(Temporal), and cheminformatics (RDKit, fingerprints). We must pick one primary
runtime language for the core before any code is written (plan step 0.1).

## Decision

The runtime is **Python** (>= 3.11).

## Rationale

The three load-bearing dependencies are all Python-native and first-class there:

- Microsoft Agent Framework's `SkillsProvider` / agent APIs,
- the Temporal Python SDK (workflows + activities),
- RDKit and the cheminformatics stack (ECFP4/DRFP fingerprints).

One language across orchestration, workflows, and chemistry avoids a polyglot
seam — no cross-process bridge or duplicated models between an orchestration
runtime and a chemistry runtime. This keeps the four layers in a single,
type-checked codebase (`mypy --strict`).

## Consequences

- Toolchain is fixed to the Python ecosystem: `uv`, `ruff`, `mypy --strict`,
  `pytest`, `pre-commit` (plan step 0.2).
- Performance-critical numerics rely on native-backed libraries (RDKit, numpy)
  rather than the interpreter.
- If a future capability has no viable Python binding, it is isolated behind an
  MCP server (its own process/language), not merged into the core.
