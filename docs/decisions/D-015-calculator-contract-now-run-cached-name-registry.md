# D-015 — Calculator contract now (`run_cached`), name-registry deferred

With three calculators sharing the same skeleton (xTB, solubility, pKa), the Rule of Three is
met, so the shared **contract** is extracted: `calc.store.run_cached` is the one place that
offloads a blocking calculator, stores the result as a plain dict, and reconstructs the typed
model — each `run_cached_*` now only derives its versioned key and delegates (DRY, plan 1c.1).
The **name→calculator registry** half of 1c.1 is deliberately *not* built: nothing dispatches a
calculator by name yet (the agent tools call each wrapper directly, and `bo.objectives` has its
own name registry). Adding a second registry now would be an abstraction with no second caller
(KISS) — it lands when a real name-dispatch consumer appears (e.g. a generic calc activity).
