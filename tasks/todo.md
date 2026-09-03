# What a helper returns

Continuation of the C/D/E follow-up. The delegation *run* the BACKLOG row asks for needs a live
model, and this environment has none — `API-KEY` is present and the provider answers
"credit balance is too low", checked rather than assumed. So this took the half of the same question
that needs no model: **is the isolation a helper exists for actually real?** That is a property of
the graph, not of a model, and nothing had ever asserted it.

- [x] Measure it. Driven on a compiled graph with a scripted helper reading ~9.8 kB: the caller's
      whole thread is **57 characters** — the `task` call and a 28-character report. Isolation holds.
- [x] Pin it, since every argument for spawning a helper rests on it.
- [x] Follow what the probe exposed: a helper's report reaches the caller's thread with **nothing
      applied to it**, because `task` returns a `Command` rather than a `ToolMessage`.
- [x] `agent/tool_result_shape.py` — one function both result-rewriting middlewares go through.
- [x] Two ADR-worthy defects fixed, each measured before and after, each test verified to fail first.
- [x] `make check` green, with the infrastructure actually up.

## Review

**The finding is one shape, and it produced two defects that had been invisible for the same
reason.** `task` returns `Command(update={'files': …, 'model_calls': …, 'messages': [ToolMessage]})`
— it has to, because a helper must write its report *and* the channels that cross the subagent
boundary in one act. Both middlewares that rewrite what the model reads opened with
`if not isinstance(result, ToolMessage): return result`, so both silently excused the one tool whose
result is unbounded prose a model wrote, while both docstrings said "every tool".

*Defect 1, measured:* a report containing `</retrieved-note-…>` reached the caller's thread with a
**live** delimiter, so everything after it read as text outside any envelope. The nonce does not
cover this the way it covers external content — a helper *copies* the tag it has just read around
its own evidence rather than guessing it, and `frame_untrusted`'s own docstring says the nonce and
the defang each cover the other's gap. Every other route by which model prose reaches a prompt
already neutralises it: the condenser defangs each field the digest model returns, the verifier
defangs the answer under review. This was the one span arriving raw.

*Defect 2, measured:* nothing bounded the report in the band between this repository's
`agent_max_tool_result_chars` (60,000) and upstream's `tool_token_limit_before_evict` (20,000 tokens
x 4 = 80,000). A 180,048-char report was offloaded by upstream to 1,599 chars; a **70,048-char**
report landed whole. After the fix: 60,312.

**Two things I got wrong on the way, both caught by measuring rather than reasoning.** I first
patched `frame_connector_results` behind its existing `isinstance` guard and re-ran the probe — the
delimiter was still live, which is what sent me to look at the return type instead of assuming the
branch had fired. And I first read the 180 kB result being cut to 1,599 chars as "the size control
works", when what had actually fired was *upstream's* offload at a different threshold; only probing
the band between the two thresholds showed the gap.

**What is deliberately not fixed.** A caller still cannot tell that a helper's report is derived from
untrusted reading. Framing it is the obvious answer and the wrong one — an envelope says "evidence to
cite", and citing a helper's summary credits a source that is this system's own paraphrase. That is a
BACKLOG row with the measurement that should come before any design, and that measurement needs a
live model.

**Gate:** `make check` green — **6275 passed, 14 skipped**. An earlier run reported 8 failures and
386 skips; the Docker daemon had died mid-run, and all 111 tests in those five files pass with
Postgres up. Reporting that rather than only the green number is the point of the rule.
