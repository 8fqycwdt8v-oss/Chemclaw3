# Sources and compute baseline (W9.3), 2026-10-22

The numbers W10–W15 of `docs/planning/sources-and-compute-programme.md` are judged against,
measured on `main` at `41ffb0865`. Three lines need a live model and are not yet measured; each says
what it waits for.

## Prefix tokens per profile

`tests/test_context_floor.py::_floor` for every registered profile, built as a deployment builds
it (`session_store="postgres"`). The ceiling is `CEILINGS["__default__"]` = 73,450; `PREFIX_BOUND`
= 83,700 adds the allowance for bundles served by the fleet.

| Profile | Total | Tool schemas | Tools bound | Prompt |
| --- | ---: | ---: | ---: | ---: |
| default | 73,121 | 64,622 | 106 | 8,499 |
| computation | 31,386 | 26,200 | 44 | 5,186 |
| design | 20,395 | 17,140 | 16 | 3,255 |
| evidence | 16,017 | 11,329 | 22 | 4,688 |
| reporting | 13,578 | 8,956 | 16 | 4,622 |
| property-lookup | 9,706 | 5,695 | 13 | 4,011 |
| safety | 7,256 | 4,714 | 10 | 2,542 |

The default profile sits 329 tokens under its ceiling, and 88% of it is tool schemas. That is the
headroom a new source's tools would have to fit into today (G4 in the programme).

Reproduce from a checkout with:

```python
from tests import test_context_floor as t
with t._as_a_deployment_runs():
    for name in sorted(t.registered_profile_names()):
        print(name, t._floor(name)[0])
```

## Files touched to add a source of a new shape

The commitments source (`commitments-json`, the last source that brought its own record shape)
arrived in `c41495159` (#280), bundled with seven other findings. Counting only the files whose
added lines name the source's own identifiers (`commitment_sync`, `commitment_tools`,
`ingest/commitments`, `commitments-json`, `review_commitments`, `074_commitments`, …):

| Where | Files |
| --- | ---: |
| The source's own package and manifest (`ingest/commitments/`, `ingest/sources/commitments-json/`) | 6 |
| Its sync workflow and tool module (`durable/commitment_sync.py`, `agent/commitment_tools.py`) | 2 |
| Core wiring outside the seam (`agent/authz.py`, `agent/chemclaw_agent.py`, `durable/background_worker.py`, `durable/schedules.py`, `ingest/sources/base.py`, `ingest/sources/registry.py`, `core/config/sources.py`) | 7 |
| Schema (`infra/sql/074_commitments.sql`, `infra/sql/README.md`) | 2 |
| Tests | 5 |
| Docs and probes | 4 |
| **Total** | **26** |

The seven core-wiring files are the number W11 is meant to bring to zero.

## Source eval

`data/evals/probes/sources.yaml`: 20 structured-filter probes (`sq-*`) and 10 named-source probes
(`ns-*`), all bucket B. Each probe's answer is computed from the corpus `Chemclaw3_mock` seeds into
the live lane (`app/eln/source_eval.py` there, pinned by its `tests/test_source_eval.py`) and
quoted in the probe's `direction:`.

**Score: not yet measured.** The run needs a model gateway (`CHEMCLAW_LLM_BASE_URL`,
`CHEMCLAW_LLM_MODEL`) for both the agent and the judge route; the environment these files were
written in has none.

## Compute eval

`data/evals/probes/compute.yaml`: 6 funnel probes and 4 recovery probes. The recoveries use
refusals the xTB tier produces deterministically today: a charged species in the gas phase, a
Hessian above `calc_hessian_max_atoms` (150) and a job above `calc_max_primitive_calls` (120).
Failures injected by a mock scheduler wait for the backend seam (ADR F) and its `slurmrestd` mock
(W9.4).

**Score: not yet measured**, for the same reason as the source eval.

## Time to first token

**Not yet measured.** It needs a model gateway, and is taken with every bundle bound.
