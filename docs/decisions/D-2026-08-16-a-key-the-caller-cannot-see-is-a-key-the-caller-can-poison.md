# D-2026-08-16-a-key-the-caller-cannot-see-is-a-key-the-caller-can-poison — what carrying out the `calc` split found

**Status:** accepted · **Date:** 2026-08-16 · Carries out
`D-2026-08-16-the-physics-leaves-the-cache-stays` on the Chemclaw3 side and records the four things
the work found that the design could not have.

## Context

The design ADR settled *what* moves: primitives to `Chemclaw3-mcp`, composites decomposed, the
D-011 cache and the durable jobs here. Doing it turned up a class of defect that the design is
blind to by construction, and it has one shape:

> **The key is derived on one side of the wire and the payload is stored on the other.** A caller
> that does not know exactly which arguments a key names can write the wrong payload under a key it
> shares with someone else — or read a payload whose meaning depended on an argument the key never
> carried. Neither failure raises.

Three instances, each measured against the running server rather than reasoned about, plus one
semantic divergence that would have deleted a capability quietly.

## The four findings

**1. `optimize_geometry` and `relax_structure` derive the same key and return different payloads.**
Measured: `xtb.opt@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:56dca3aa944bd3da`
for `CCO` on both. One returns an `OptimizationSummary` — no coordinates — and the other the full
`OptimizationResult` with the geometry every downstream calculation is keyed on. Caching either
under that one key poisons the other, and the failure surfaces as a validation error on a *hit*,
deep inside a reaction job, long after the tool call that wrote it. **Resolution: one key, one
payload shape.** The `optimize_geometry` tool embeds and calls `relax_structure`, then drops the
geometry locally — where dropping it costs nothing. The server's one-shot tool is never used from
here.

**2. A Fukui key does not name the mode, and the server re-ranks on the way out.** Measured: all
three modes on phenol derive `xtb.fukui@…:3aaf5b0543327fb5:b41312b0cdc59ab7`. That is *correct* —
the three single points do not depend on the mode, which only chooses the sort — and it is exactly
why the in-process version re-ranked a cached result. The split broke it silently: a remote call is
always right because the server sorts before answering, and **a cache hit never reaches the
server**, so the second mode asked for would be served the first mode's ordering carrying the first
mode's `mode` and `ranked_by` labels. A confidently wrong regiochemistry answer with nothing raising
anywhere. **Resolution:** `SiteReactivityResult.ranked_for` re-ranks after the cache, `mode` is not
sent at all, and the test ranks `f_minus` and `f_plus` in opposite orders so a mis-served ranking
cannot look like a coincidence.

**3. `multiplicity=None` means the opposite on the two sides.** Here it meant *derive it from the
SMILES' radical electrons*; on the server it means *closed-shell singlet*. Measured: `[CH3]` with
`multiplicity=None` is refused as "9 electrons at charge 0 cannot be a closed-shell singlet". Sent
as-is, every homolysis reaction — the one class whose entire point is an open-shell side — would
have failed at the embed with an error naming the wrong thing. **Resolution:**
`connectors/calc/compose.py::radical_multiplicity` derives it here and passes it explicitly. It
belongs here anyway: it is a property of the molecular graph and needs no engine.

**4. The version a prediction is logged under comes off the payload, not from a derivation.** The
design ADR already forbade deriving a `calc_version`; carrying it out showed there are *two*
questions, and only one of them is "what version is current". `_log_prediction` needs the version
the number in hand was computed under — which on a cache hit is **not** the current one — and that
is what the calibration ledger's `(calc_type, calc_version, input_hash)` key means. So the tools
read `payload["calc_version"]`, and only `calculator_trust`/`calculator_outliers` — which have no
result to read one off, because a trust report is the question — pay a `calculation_key` round trip
(`remote_version`, ~0.11 s measured).

## Consequences for the tree

**The engine modules are consolidated, not stripped in place.** Twenty modules held the physics;
their surviving pydantic models are now three files. `science/calc/models.py` is every shape the
cache reconstructs and the Temporal wire carries; `science/calc/thermo.py` is the statistical
mechanics that had to stay (RRHO over a Hessian, Boltzmann over an ensemble — both depend on a
temperature the expensive half never saw, which is the whole reason the composites were
decomposed); `science/calc/logd.py` is the Crippen sum, the Henderson-Hasselbalch term and the site
enumeration its domain check reads. The alternative — twenty files stripped to a model apiece —
would have left the tree's own map naming `xtb_engine`, `xtb_cli`, `crest_cli` and `anc`: programs
that do not run here. `tests/test_docstring_paths.py` exists because a map that points at nothing is
worse than no map.

**`science/bo` takes its calculators as arguments.** `featurize_problem` and `solubility_objective`
called the calculators directly, and the client that reaches them now lives one package up, which
`chemclaw.science` may not import. Excusing that edge would have declared a
`science ↔ connectors` cycle to save one argument at three call sites, so instead `science/bo`
declares two callable seams (`PropertiesFor`, `LogSFor`) and `connectors/bo/calculators.py` binds
them. The property both callers always advertised — a molecule seen before is never recomputed — is
unchanged and is now the client's.

**One new setting.** `calc_version_probe_smiles` (acetic acid) is the molecule `remote_version`
derives a key *for*. A `calc_version` is a property of the programs and the calibration behind a
calculator, not of a molecule, but `calculation_key` answers an identity and an identity is of
something. Configuration rather than a literal, so it is one visible fact rather than a repeated
constant.

**`connectors/calc/server/app.py` has no `on_start` hook.** It existed for one trap: three call
sites reached `pka_calc_version()` without threading it, and that shelled out to `xtb --version` on
the first call in a process — so an ordinary first `calculator_trust("pka")` in a fresh pod could
hold the connector's single event loop for the 30 s subprocess timeout. There is no binary to ask
any more. Keeping the hook to warm something would be a diagnostic pretending to be a guard.

## What was measured

Against the live server on 127.0.0.1:8860, with a fresh in-memory store per row. `computed` counts
remote calculations actually performed; the repeat column is the same call again.

| | cold | repeat |
|---|---|---|
| `compute_thermochemistry(CCO)` | 0.856 s, **2 computed** | 0.372 s, **0 computed** |
| `compute_thermochemistry(CC(=O)OCC)` | 2.060 s, **4 computed** | 0.626 s, **0 computed** |
| `compute_thermochemistry(ibuprofen)` | 11.469 s, **2 computed** | 0.448 s, **0 computed** |
| `predict_logd(pyridine)` | 0.824 s, **1 computed** | 0.107 s, **0 computed** |

The cold column reproduces the design ADR's in-process baselines (0.816 s and 3.273 s), so the
server is not the slower place. **D-011 holds across the wire**: a persisted result is never
recomputed, which is the `0 computed` column and not the clock. What the clock shows instead is the
new floor — the repeat is round trips and nothing else, at ~0.11 s each, because a session cannot
be shared process-wide (`connectors/identity.py`: two concurrent callers over one session
misattribute each other). `CC(=O)OCC` computing **4** cold is the saddle-point refinement loop
taking its second pass, which is the behaviour that made thermochemistry uncacheable as a whole.

Ethyl acetate's second pass and the ibuprofen row together are the case for decomposition rather
than for shipping: a composite whose key would name the geometry the loop settles on would have
turned that 11.5 s into 11.5 s again, every time.

## Consequences for the suite

Fifteen physics test files were deleted — that coverage lives in `Chemclaw3-mcp` now — and what
proved a property of *this* repository was kept and rewritten against
`tests/calc_server_fake.py`, a stand-in that reproduces the three key properties above so a test
cannot pass on a design that fails in production. The measured physics that had to survive offline
is `tests/test_calc_thermo.py`: real `compute_hessian` payloads for water, CO2 and H2 recorded from
the live server into `tests/fixtures/calc_hessians.json`, checked against NIST standard entropies
(45.10 / 51.06 / 31.23 cal·mol⁻¹·K⁻¹; computed 45.05 / 51.20 / 31.31). That fixture also pins the
transport: a change to the base64 `.npy` encoding on either side turns it red instead of silently
producing a spectrum of zeros.

`tests/test_calc_payload_schemas.py` changed meaning rather than contents. This repository no longer
*writes* those payloads, so the digests now guard the **reader** half of a cross-repository
contract — adding a required field here makes every row already on disk fail to validate. Neither
side can see the other's schema, which is precisely why it is checked.
