# `science/bo/benchmarks` — a real reaction dataset, wrapped as an optimization problem

`reizman_suzuki.py` turns the Reizman et al. Suzuki–Miyaura screening data into an
`OptimizationProblem` plus an async objective, by fitting a light RandomForest surrogate over the
discrete experimental grid — the same idea as Summit's `ExperimentalEmulator`, in a Python-3.11
stack. `objectives._reizman_suzuki` registers it under the name a durable campaign resolves.

`data/` holds the vendored CSV and its `NOTICE.md`. It is the one corpus in this repository that
lives inside `src/` rather than under `data/`, and that is argued rather than overlooked: it is
package data pinned to the surrogate that reads it, not something an operator configures — swapping
it would silently change what the registered `reizman_suzuki` objective means. `ARCHITECTURE.md`
names the exception and `tests/test_repo_map.py` holds it to exactly one file.
