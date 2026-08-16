# Repro-lens verdicts — `mcp-chem-rxn-kit--design.md`

Scope: findings marked **critical** or **high** only. The file contains exactly one such finding
(the `manifests/` one, `high`); the other nine are medium/low and are out of scope, unexamined.

Every number below is mine. I did not run the reporter's scripts; all four probes
(`/tmp/repro_shadow.py`, `/tmp/repro_surface.py`, `/tmp/repro_ripple.py`, `/tmp/repro_escape.py`)
were written from the source.

---

## `manifests/` is the discovery directory *and* contains the one bundle that must not be discovered

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. The cited code is real and current.** `test_fleet.py:70` is indeed
`def test_the_manifest_is_registered_by_symlink`. In the backend, `registry.py:108` is
`def _bundle_dirs()`, `:125` is its `return`, `:166` is `def enabled()`, `:175` reads
`names = settings.connectors_enabled_list` — the cited ranges bracket exactly the two functions the
finding relies on. `manifests/calc/connector.yaml` is a symlink to `../../servers/calc/connector.yaml`.
The reporter's two greps reproduce: mcp `calc` `^jobs:` → `0`, core `calc` `^  - name:` → `5`.

**2. The shadow reproduces.** Driving the backend's own registry with the README's recipe
(`manifests:` ahead of the shipped connectors dir), nothing else changed:

```
MODE: plain
  calc bundle dir : /home/user/Chemclaw3/src/chemclaw/connectors/calc
  calc jobs       : ['compute_reaction_energy', 'compare_solvents', 'scan_coordinate',
                     'sample_conformers', 'compute_interaction_energy']
MODE: shadowed
  calc bundle dir : /workspace/chemclaw3-mcp/manifests/calc
  calc jobs       : []
```

Diffing the two enabled surfaces (`/tmp/repro_ripple.py`) gives the exact loss:

```
calc: LOST tools=['calculator_outliers', 'calculator_trust', 'compute_thermochemistry',
                  'fetch_artifact', 'find_calculations', 'list_artifacts', 'report_measurement']
calc: LOST jobs =['compare_solvents', 'compute_interaction_energy', 'compute_reaction_energy',
                  'sample_conformers', 'scan_coordinate']
```

Across the whole durable surface that is **7 job names → 2**. The finding named five vanishing jobs;
seven tools go with them — the calibration ledger (`report_measurement`, `calculator_trust`,
`calculator_outliers`), the artifact store (`list_artifacts`, `fetch_artifact`), the cache read
(`find_calculations`) and `compute_thermochemistry`. That matches the loss `servers/calc/connector.yaml`'s
own header comment predicts, so the two repos agree on the hazard and neither enforces anything.

**3. It is genuinely silent.** `registry.enabled()` returned 9 bundles and raised nothing.
`_bundle_dirs` is a bare `found.setdefault(path.name, path)` with no logger call anywhere in the
collision path — the module's only `logger.warning` is at `:524`, unrelated. The API lifespan's only
connector-related startup work is `check_connectors_at_startup`, a *reachability* probe; it has no
notion of which manifest won a name. And since the shadowed manifest moves the calc endpoint from
`127.0.0.1:8815` to `127.0.0.1:8860` — the address of the mcp fleet's own calc server, which an
operator pointing at `manifests/` is by construction running — even that probe goes green. No error,
no warning, no failed startup, as claimed.

**4. The test suite really does pin the hazardous state.** Removing the directory and running the
fleet suite:

```
$ mv manifests/calc /tmp/… && uv run pytest tests/test_fleet.py -q
FAILED tests/test_fleet.py::test_the_manifest_is_registered_by_symlink[calc]
1 failed, 37 passed
```

Exactly one failure, exactly the test named. (Restored; `git status --porcelain` clean.)

### Why

Every load-bearing element re-derived independently: the symlink, the zero-vs-five job counts, the
precedence rule, the silence, and the test that blocks the obvious fix. I went looking for four
things that would have killed or softened it, and none of them exist:

- **A loud failure somewhere upstream.** There is none. Skill frontmatter naming the vanished jobs
  is only checked by `make skill-validate`, a CI gate, not at startup.
- **`connectors_enabled` as an escape.** It is not one. Precedence is resolved in `discovered()`,
  *before* enablement reads it, so naming `calc` explicitly still yields the mcp manifest:
  `explicit enable-list, calc jobs: []`, `url: http://127.0.0.1:8860/mcp`. The only escape is to not
  follow the README's recipe — which offers no filtered alternative.
- **Collateral that would make the finding overstated in the other direction.** `chem` and `safety`
  also collide by name, and the ripple diff shows they lose *nothing*. The README's claim that those
  two overrides are equivalent ports holds; the finding is correctly scoped to `calc` alone.
- **A ripple the reporter missed that makes it worse.** I checked whether `ToolScopedSkills` would
  also take skills dark: `skills visible plain: 25`, `shadow: 25`, `go DARK: []`. It does not —
  each affected skill declares at least one surviving tool, and the narrowing hides a skill only when
  *every* declared tool is absent. Worth recording so nobody inflates the blast radius later.

On severity: the README's warning paragraph is the entire mitigation, and under this audit's own
rule a comment asserting safety is a claim, not a control — especially one placed three paragraphs
*below* the code block that causes the harm. The trigger is an operator following this repo's
documented instruction verbatim; the outcome is silent capability loss that only surfaces as
"the agent stopped being able to do thermochemistry". **High stands.** The reporter's proposed fix
(split the discovery surface from the one-manifest-per-server invariant) addresses the actual
mechanism rather than adding more prose, which is the right shape.
