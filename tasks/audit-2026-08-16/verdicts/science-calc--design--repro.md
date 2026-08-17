# Verification — `science/calc` design findings, reproduction lens

Verifier: adversarial re-derivation, from source, at `HEAD = 8573569`. Two findings in scope
(severity **high**); the remaining seven are medium/low and were not examined.

Method note on hygiene: I never edited the shared checkout. Both deletion experiments were run
against a *copy* of `src/` in `/tmp/mut` and `/tmp/mut2`, selected at import time with
`PYTHONPATH=` — the editable install is a plain `.pth` adding `/home/user/Chemclaw3/src`, so a
`PYTHONPATH` entry wins (verified: `PYTHONPATH=/tmp/mut/src uv run python -c "import chemclaw"` →
`/tmp/mut/src/chemclaw/__init__.py`). Nothing under `/home/user/Chemclaw3` was mutated.

---

## Five request models in `models.py` have no reference anywhere in the repository

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

**What I did**

1. Re-grepped, wider than the reporter did — every file in the tree, no extension filter, only
   `.git`/`.venv` excluded:

   ```
   $ grep -rn -E "XtbInput|PkaInput|SolubilityInput|DescriptorInput|LogdInput" . \
       --exclude-dir=.git --exclude-dir=.venv
   ```

   Outside `docs/archive/`, `docs/decisions/D-095…` (prose) and `tasks/audit-*`, the only hits in
   the entire repository are the five `class` statements themselves:
   `models.py:160, 182, 212, 237, 266` — the exact lines cited, and all five are current.
   No `connector.yaml` mentions any of them (`grep "Input" --include=*.yaml src/` → nothing;
   the only `params_model` strings are `science.bo.problem:CampaignSpec` and
   `connectors.calc.specs:*JobSpec`).

2. Static grep cannot see dynamic access, so I checked at runtime. `/tmp/probe_inputs.py` imports
   **every** module under `chemclaw` with `pkgutil.walk_packages`, then scans every loaded
   `chemclaw.*` module namespace, every pydantic `model_fields` annotation, and
   `__subclasses__()`:

   ```
   modules imported: 341
   import failures: 0
   namespace hits: []
   annotation hits: [bofire CategoricalDescriptorInput ...]   # substring false positives only
   subclasses: {'LogdInput': [], 'PkaInput': [], 'XtbInput': [], 'SolubilityInput': [], 'DescriptorInput': []}
   ```

3. Confirmed the reporter's supporting claims. `PAYLOAD_MODELS`
   (`tests/test_calc_payload_schemas.py:88-99`) is nine result models plus `QMJobResult` — no
   `*Input`. `compute_xtb_energy(smiles: str, charge: int = 0)` at
   `connectors/calc/server/tools.py:640` takes plain arguments, and the module's
   `from chemclaw.science.calc.models import (...)` block (`:48-59`) imports ten names, none of
   them an `*Input`.

4. Tested the "behaviour-preserving" half rather than assuming it. Deleted all five classes by AST
   in `/tmp/mut/src` (28 lines) and ran the **whole** suite against it:

   ```
   $ PYTHONPATH=/tmp/mut/src uv run pytest -q -x -p no:randomly
   1 failed, 2313 passed, 99 warnings in 326.08s
   ```

   The single failure is `tests/test_live_storm.py::test_the_lane_scripts_the_chaos_family_drives_exist`,
   which is my scaffolding, not the deletion: `live_storm._LANE_DIR = Path(__file__).resolve()
   .parents[3] / "infra" / "live"` — under `/tmp/mut/src` that path does not exist. The same test
   file has one more `_LANE_DIR` reader that fails for the identical reason, and both pass on the
   real tree.

5. Checked the cross-repo direction the reporter asserted but did not test. `Chemclaw3-mcp`'s
   `pyproject.toml` declares `dependencies = []` at the root and no member depends on `chemclaw`,
   and its calc server carries its *own* `SolubilityInput`
   (`servers/calc/src/chemclaw_mcp_calc/tools.py:229`). So nothing outside this repo imports these
   five either, and the fix's "the contract belongs in the repo that serves it" is already the
   state of the world there.

**Why**

Every factual claim in the finding reproduces exactly — the symbols, the line numbers, the absence
of importers, the absence of dynamic reference, and the claim that deletion is behaviour-preserving
(2,313 tests green with the classes gone). Nothing here is refuted.

What does not hold is **high**. There is no runtime path, no wrong answer, no reachable trigger and
no consequence beyond ~28 lines a maintainer has to read past; the stated harm ("a reader adding a
tool will reasonably assume `PkaInput` is the contract") is speculative and, if it happened,
self-correcting at the first call — the MCP tool signatures are plain arguments and any attempt to
wire against `PkaInput` fails immediately and locally. For calibration: the same reporter files
`find_calculations` advertising an unmatchable `calc_type` — which makes the model report "we have
never computed descriptors for this molecule" as fact to a chemist — at **medium**. Dead, unimported
type declarations cannot outrank a live false statement to a user. This is a low-severity cleanup,
and a good one.

---

## `uncertainty.structural_domain` is dead code kept alive by its own test

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

**What I did**

1. Re-grepped the whole tree (all extensions, `.git`/`.venv` excluded):

   ```
   src/chemclaw/science/calc/uncertainty.py:61:_ORGANIC_ELEMENTS = frozenset({...})
   src/chemclaw/science/calc/uncertainty.py:161:def structural_domain(mol: Chem.Mol) -> ...
   src/chemclaw/science/calc/uncertainty.py:190:    foreign = ... - _ORGANIC_ELEMENTS
   tests/test_uncertainty.py:26,47,59,70,77
   ```

   Nothing else. Line numbers 61/161 are current, and the docstring paragraph the fix proposes
   deleting does begin at line 34 (`**On what a domain check may honestly assert here.**`).

2. Confirmed there is no runtime path either. The 341-module import probe above finds no module
   holding a reference; the only `Estimate(...)` construction in `src/` is
   `connectors/qm/knowledge.py:44`; and `predict_solubility`
   (`connectors/calc/server/tools.py:660-683`) is a `cached_remote` call whose payload is
   `SolubilityResult.model_validate(payload)` — the `estimate` field is whatever the server sent.
   Nothing in this process computes a domain flag.

3. Ran the deletion. Removed `structural_domain` and `_ORGANIC_ELEMENTS` (37 lines) in
   `/tmp/mut2/src` and ran the full suite with `tests/test_uncertainty.py` ignored (the finding's
   own fix deletes those four assertions):

   ```
   $ PYTHONPATH=/tmp/mut2/src uv run pytest -q -p no:randomly --ignore=tests/test_uncertainty.py
   6 failed, 4051 passed, 1 skipped in 991.71s
   ```

   All six failures are attributable and none touches the deletion: four are
   `tests/test_prose_contract.py` and one is `tests/test_live_storm.py`, all of which derive the
   repository root from `__file__` and therefore look for `docs/`, `Makefile` and `infra/live/`
   under `/tmp/mut2` — `uv run pytest tests/test_prose_contract.py` on the real tree gives
   `33 passed in 3.57s`. The sixth, `tests/test_reizman.py::test_bo_campaign_finds_high_yield`, is a
   **pre-existing** wall-clock timeout: it fails the same way on the unmutated tree
   (`1 failed, 2 passed in 186.86s`, the harness itself printing "these are wall-clock caps, not
   assertion failures").

4. Settled the consequence claim against the other repository rather than against this one's prose.
   `Chemclaw3-mcp` has its own copy and it **runs**:

   ```
   servers/calc/src/chemclaw_mcp_calc/engine/solubility.py
     26: from chemclaw_mcp_calc.engine.uncertainty import Estimate, structural_domain
    152:     in_domain, reasons = structural_domain(mol)
    168-169:  in_domain=in_domain, domain_reasons=reasons,
   ```

   and its `predict_solubility` docstring tells the caller to read `estimate.in_domain`
   (`servers/calc/.../tools.py:216`).

**Why**

The dead-code half reproduces perfectly: one definition, zero production callers, no dynamic
reference, and a 4,051-test run green with the function and its constant removed. That part is
solid and the fix is safe.

The exaggeration is the framing that carries the "high". The finding invokes the `reject_widening`
pattern — "a guard with no caller … is a claim that a control exists" — but that pattern's harm is
that *the control does not run anywhere*. Here it does: I read the mcp server's
`engine/solubility.py` and the domain check executes on the molecule ESOL is actually handed, which
is the only place it can honestly run. So there is no control gap, no user-visible defect, and no
reachable trigger — the residue is duplicated prose and 37 unreachable lines. The one real (small)
hazard the reporter identified is the module docstring's present tense about `predict_solubility`,
a function that is not in this repository; that is a documentation defect worth fixing alongside the
deletion, not a high-severity finding.
