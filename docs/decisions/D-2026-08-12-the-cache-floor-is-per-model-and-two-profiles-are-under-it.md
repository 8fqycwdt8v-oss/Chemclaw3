# D-2026-08-12-the-cache-floor-is-per-model-and-two-profiles-are-under-it — The minimum cacheable prefix, measured

**Supersedes the minimum-prefix claim in
[D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it](D-2026-08-12-the-prefix-is-static-so-stop-paying-for-it.md).**
Everything else in that ADR stands — the breakpoints, the middleware choice, the `cache_creation`
key the ledger was not reading. What is corrected here is one sentence it wrote from the spec, and
the conclusion that sentence supported.

## What that ADR claimed, and where it came from

> Below the provider's minimum cacheable prefix (~1,024 tokens, and **not monotonic** across
> models — 512 on the newest, 4,096 on some) the breakpoint is accepted, no entry is created …

and, three lines earlier, that our prefix is "far above every model's minimum cacheable size".

Every other number in that ADR was measured. This one was not, and it entered the tree three times
in three mutually inconsistent forms — `1,024`/`2,048–4,096` in the `llm_provider` docstring,
`1,024`/`512`/`4,096` in the ADR, `1,024`/"higher on some models" in `.env.example`. Nothing read
any of them at runtime and no test pinned any of them, so no gate in this repo could see that the
three disagreed.

## What it is

Measured 2026-08-12 by bisecting a synthetic prefix to ±1 token and reading
`cache_creation_input_tokens`, each probe carrying a nonce at its head so it could not read the
previous probe's entry:

| model | floor |
|---|---:|
| `claude-sonnet-5` | **1,024** |
| `claude-haiku-4-5-20251001` | **4,096** |

Both boundaries are exact and both landed on a power of two. Monotone within each model across a
seven-point scan either side, so "threshold" is the right shape — that was an assumption worth
checking rather than asserting, and it held.

**The direction is the trap.** "Not monotonic across models" was right in spirit and useless in
practice, because it did not say which way. The smaller, cheaper model has the **four times
higher** floor. Any rule of the form "newer or cheaper ⇒ lower minimum" is wrong here, and
`agent_model` defaults to sonnet while the live probe lane pins haiku — so the two models this
system actually runs on sit at opposite ends of the range.

## Which of our prefixes clear it

The cacheable prefix is `tools` + `system`, which is what upstream's breakpoints cover. Counted
with `count_tokens` on payloads captured from `build_langgraph_agent`:

| profile | prefix | vs haiku's 4,096 |
|---|---:|---|
| `default` | 21,321 | 5.21× |
| `computation` | 8,708 | 2.13× |
| `reporting` | 7,490 | 1.83× |
| `evidence` | 5,803 | 1.42× |
| `design` | 5,625 | 1.37× |
| `property-lookup` | 3,092 | **0.75× — never caches** |
| `safety` | 2,933 | **0.72× — never caches** |

Confirmed end to end rather than inferred from the synthetic boundary, by replaying each profile's
own captured payload twice:

```
safety            tools= 3  call1 input= 2958 write=    0 read=    0   call2 input= 2958 write=   0 read=    0
property-lookup   tools= 2  call1 input= 3116 write=    0 read=    0   call2 input= 3116 write=   0 read=    0
computation       tools= 9  call1 input= 8737 write= 8734 read=    0   call2 input= 8737 write=   0 read= 8734
```

The control matters as much as the finding: a run where nothing caches would prove the harness
broken rather than the profiles below-floor.

**Both below-floor profiles are above sonnet's 1,024.** So whether a narrow profile caches is
decided by `model_routes`, not by the profile — a deployment can move a profile across the boundary
without touching it, and nothing in the tree said so.

## The decision

**Nothing enters the code path, and the two profiles are not enlarged.**

- *No threshold check.* The original argument — that a provider threshold copied into this repo
  would be a second, staler statement of a number only the provider knows — is *strengthened* by
  the measurement, not weakened: the number moved by 4× between two models of one generation. What
  changes is that the number is now written down as measured, with its date and method, instead of
  half-remembered from a spec in three contradictory places.
- *No padding.* Enlarging a 2,933-token prompt to clear a 4,096-token floor would spend tokens to
  save tokens and would corrupt the prompt to serve the biller. The provider's boundary is not a
  defect in a narrow profile.
- *A ratchet instead.* `tests/test_prompt_caching.py::test_which_shipped_profiles_clear_the_cache_floor`
  pins the *set* of below-floor profiles rather than any prefix size. It costs nothing —
  `count_tokens` is not billed — and it fails if a prompt edit pushes `design` (1.37×) or
  `evidence` (1.42×) under, which is a cost regression with no symptom except the bill. Mutation-
  checked against the old believed floor of 1,024: under that number the test reports no profile
  below, which is exactly the false belief it now prevents.
- *An operator-visible reading.* `chemclaw_cache_write_tokens_total` carries a `profile` label. A
  profile with input tokens and no cache series is one under the floor. That detector already
  existed; what was missing was anyone knowing to look.

## What this cost, and what it bought

Twenty-two calls on haiku and twenty-three on sonnet, ~$0.27 all in. Against that: the tree carried
three disagreeing statements of a number, the one nobody had run, and it was wrong by 4× for the
model every live probe uses. The general lesson is the one this repo already writes down — prose is
evidence about its author's belief, never about the system — and the specific one is narrower: a
number taken from a vendor's documentation is not a measurement, and it ages differently from the
code around it, because nothing recompiles when the vendor changes it.
