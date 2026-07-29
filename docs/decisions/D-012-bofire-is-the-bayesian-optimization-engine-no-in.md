# D-012 — BoFire is the Bayesian-optimization engine (no in-house BO), pulled forward

Optimization campaigns use the fast predictors + store as objective evaluations. We adopt
**BoFire** (domain modelling + BoTorch strategies) behind a thin adapter rather than building our
own BO; BoFire types stay encapsulated and never leak into the agent/skill. BO is pulled forward
from "defer until measured" because it drives which calculations are worth running. Plan Phase 1d.
