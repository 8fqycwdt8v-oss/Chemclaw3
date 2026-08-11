# M12 · team routing accuracy and per-specialist token cost

Routing accuracy is scored over the turns that were *delegated*, and token cost over the turns the ledger could be asked about. Both denominators are printed, because the two failures they separate — never delegating, and delegating wrongly — have different fixes.

| arm | probes | delegated | correct | accuracy | tokens | unmeasured turns |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| team | 15 | 0 | 0 | 0% | 0 | 15 |

**Only one arm has run.** Re-run with `--arm single` against a front door configured the other way (`CHEMCLAW_AGENT_TEAMS_ENABLED`) — a routing number with nothing to compare it against does not answer the question M9 deferred.
