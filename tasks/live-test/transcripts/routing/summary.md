# M12 · team routing accuracy and per-specialist token cost

Routing accuracy is scored over the turns that were *delegated*, and token cost over the turns the ledger could be asked about. Both denominators are printed, because the two failures they separate — never delegating, and delegating wrongly — have different fixes.

| arm | probes | delegated | correct | accuracy | tokens | unmeasured turns |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 15 | 0 | 0 | 0% | 1779642 | 0 |
| team | 15 | 2 | 2 | 100% | 1745087 | 0 |

## team · per specialist

| specialist | turns | tokens | tokens/turn |
| --- | ---: | ---: | ---: |
| safety | 2 | 208691 | 104345 |

## Turns the supervisor answered itself

Scored against the surface the question was declared to belong to, so a run that delegates little still measures something. This is a claim about the corpus's partition — *was `expects_specialist` the right answer* — and not about the supervisor's judgement, which only the delegated turns above can speak to.

| arm | self-answered | within expected surface | share |
| --- | ---: | ---: | ---: |
| single | 12 | 10 | 83% |
| team | 10 | 8 | 80% |
- **single** rt-13 reached outside its surface: find_past_jobs, gather_evidence
- **single** rt-14 reached outside its surface: gather_evidence
- **team** rt-13 reached outside its surface: find_past_jobs, gather_evidence
- **team** rt-14 reached outside its surface: gather_evidence
