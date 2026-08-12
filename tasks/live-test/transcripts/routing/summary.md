# M12 · team routing accuracy and per-specialist token cost

Routing accuracy is scored over the turns that were *delegated*, and token cost over the turns the ledger could be asked about. Both denominators are printed, because the two failures they separate — never delegating, and delegating wrongly — have different fixes.

| arm | probes | delegated | correct | accuracy | tokens | unmeasured turns |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 15 | 0 | 0 | 0% | 1759062 | 0 |
| team | 15 | 0 | 0 | 0% | 0 | 15 |
