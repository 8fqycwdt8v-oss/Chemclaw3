---
id: bo-regret-reizman
metrics: [bo_regret]
output:
  best_value: 95.3
  direction: maximize
reference:
  optimum: 98.7
---
Optimization-progress case: the BoFire BO campaign (`bo`) over the Reizman–Suzuki
benchmark (`bo/benchmarks/reizman_suzuki.py`), which maximizes Suzuki-coupling yield
(%). The dataset's best observed yield is 98.7 % — the reference optimum. A campaign
that converges to a best-found yield of 95.3 % has a regret of 98.7 − 95.3 = 3.4 %.

`bo_regret` is a progress metric with no pass threshold (it is cited in a report, not
gated), so this case exists to keep the registered metric under the versioned case-set
(plan step 1d.6): a change that breaks `bo_regret` fails `make eval`. Recompute
`best_value` from the campaign's `CampaignResult.best` when the surrogate or strategy
changes; this file records the output under evaluation at its version.
