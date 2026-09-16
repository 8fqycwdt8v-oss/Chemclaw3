# D-2026-09-16-a-library-already-in-the-closure-is-a-declaration-not-a-dependency — six adoptions, and what measuring each one cost

A read-only audit across this family asked one question of every module: is this hand-written code a
library already does better? Most of the answers were no, and the tree said so in its own docstrings.
This records the ones where the answer was yes, and — more usefully — what each turned out to be once
it was measured rather than proposed.

The unifying property: **every library adopted here already resolved in `uv.lock`.** `httpx-sse` via
`mcp`, `pathspec` via `mypy`, `charset-normalizer` via `requests`, `tiktoken` via `langchain-openai`.
Adopting them added zero bytes to any image; what it cost was a declaration line, and this
repository's rule that a directly-imported library is declared rather than left transitive is the
whole of the change on the manifest side. `tiktoken` had already demonstrated why that rule exists:
nothing here holds imports against the manifest, so a `langchain-openai` bump dropping it would have
degraded the token budget to its estimator **in silence**, the fallback being by design never an
error.

## What each one turned out to be

**`httpx-sse`** — three first-party decoders that disagreed (`[6:]` off `"data: "`, `[5:].strip()` off
`"data:"`, and a third form), none handling multi-line `data:`, `id:` or `retry:`. The multi-line gap
is **latent**: `sse_starlette` serialises with `model_dump_json()`, which emits no raw newline, so it
has never fired against this system's own server. Adopted anyway, because a harness that misreads a
legal frame reports *the system* as dropping events. The real behaviour change is elsewhere: a 200
that is not `text/event-stream` — a proxy answering instead of the front door — is now a named
transport failure where it used to book a silent unanswered turn.

**`pathspec`** — `fnmatch` gives `**` no special meaning, so the crawler tried each pattern three
ways. The recorded defect was a top-level `Archive/` being indexed. The *unrecorded* one is larger and
is the reason to change: all three arms return `False` for a **directory**, so an excluded subtree was
never pruned, only re-rejected once per file, on a share whose own docstring is about the cost model
of a terabyte.

**`charset-normalizer`** — the share was decoded as UTF-8 with `errors="replace"`, so cp1252 `60 °C`
became mojibake that was chunked, embedded and citable with nothing counted. The ordering is the
safety argument: `utf-8-sig` strict first means every file that decodes correctly today is
byte-identical, and a file detection can damage is one strict UTF-8 already refused.

**`tiktoken`** — and here the audit's own premise was wrong. It proposed making the tool schemas exact
as "the largest estimated term", 89% of the prefix. Measured: chars/4 is **0.05%** off on JSON schemas
and **18% high** on the system message. The whole correction is the English prose. The per-turn thread
count was then *refused* even though the thread is where chars/4 is most wrong (2.08x), because an
exact count there is 24.2 ms against 0.03 ms, three times per model call, two of them on the event
loop that serves every SSE stream.

**`bisect`** — and the obvious form is wrong. `bisect_right(range(len(text) + 1), ...)` looks right and
is not: `text[-0:]` is the whole string, so `key(0)` is the *maximum* and the sequence is unsorted.
Over `range(1, ...)` the keys are non-decreasing and the off-by-one that once returned 100,024
characters at a budget of 0-2 becomes **unrepresentable rather than guarded**, which is the general
shape worth keeping: prefer a search space that cannot express the failure over a guard that catches
it.

**`networkx.utils.UnionFind`** — the cleanest instance, a textbook algorithm re-typed. The replacement
is *stronger* (union by weight, which the hand-rolled form lacked) and identity was measured over
30,000 random cases rather than asserted.

## The rule the whole exercise produced

**An adopted API's defaults are part of its surface, and they are what a review misses.** The
sharpest near-miss in this wave was not a library that fit badly; it was one that fit perfectly and
disagreed about a default. `rdSubstructLibrary.GetMatches` defaults `useChirality=True` where
`HasSubstructMatch` defaults it to `False` — taking the default turned a chiral query matching **514**
molecules into **0**, which reaches a chemist as "no precedent exists". Nothing about the adoption
looked wrong; only running both did.

So: when replacing a call with an upstream equivalent, diff the **defaults**, not the semantics you
assume they share, and pin the disagreement in a test so a future release aligning them goes red
rather than leaving an unjustifiable argument in the call.

## What keeps it true

- `tests/test_live_probes.py`, `tests/test_live_storm.py`, `tests/test_live_benchmark.py` — one decoder
- `tests/test_document_share.py`, `tests/test_document_formats.py` — the exclusions and the decode
- `tests/test_context_budget.py`, `tests/test_compaction.py` — the exact prefix and the refused thread
- `tests/test_agent_observability_model.py`, `tests/test_message_pairing.py`
- `tests/test_molfp.py::test_a_chiral_query_is_matched_the_way_the_loop_matched_it`
- `tests/test_third_party_layering.py` — every new root mapped to a stack
