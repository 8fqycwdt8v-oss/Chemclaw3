# D-2026-08-13-the-guard-must-not-refuse-a-dependency-bump — The checkpoint stamp covers the channels this repository declares, and refuses only the direction that was measured to fail

**Status:** accepted · **Date:** 2026-08-13 · Supersedes §2 and §3 of
`D-2026-08-13-a-checkpoint-says-which-schema-wrote-it` (the stamp's *content* and the claim about
what it catches). §1, §4 and §5 of that decision stand: the stamp still lives in checkpoint
metadata, a mismatch still refuses rather than silently starting the thread over, and an unstamped
checkpoint is still accepted.

## Context

The guard shipped in #173 fingerprints `get_type_hints(ChemclawState, include_extras=True)` — every
channel the state class reports — and refuses a checkpoint whose fingerprint differs from this
build's. Measured on this tree:

```
fingerprint: bf5b523b8e62
channel count: 6
channels: ['jump_to', 'loop_capped', 'messages', 'model_calls', 'structured_response', 'todos']
```

Four of those six are langchain's. `ChemclawState` extends `PlanningState`, and a `TypedDict`
**merges its bases' annotations into its own `__annotations__`** — so `ChemclawState.__annotations__`
reports all six as well, and there is no attribute that reports only the two this repository wrote.
The prior ADR read that inclusion as a feature ("a dependency bump that reshapes the upstream
`AgentState` / `PlanningState` moves the fingerprint with it") and concluded that the fingerprint
"never fires falsely".

Both halves are wrong, and the first one inverts the guard's purpose: a routine `langchain` minor
bump that adds or renames one of its own channels moves the fingerprint, so **every in-flight thread
in the fleet is refused** — the guard bricking live sessions on a dependency change nobody
associated with turn state, which is the exact harm it was built to prevent.

The second question was which *direction* of a schema change to refuse. The prior ADR assumed a
moved channel name, and its repro test asserted `KeyError: 'todos'` from a turn-boundary resume.
That test had no control. Measured, with the control:

| change, resumed on a stored thread | at a turn boundary | resumed mid-turn (`interrupt()`) |
| --- | --- | --- |
| rename (`plan` → `todos`) | `KeyError: 'todos'` — **and on a fresh thread too** | `KeyError: 'todos'`, fresh thread `OK` |
| added channel a node indexes | `KeyError: 'extra'` — and on a fresh thread too | `KeyError: 'extra'`, fresh thread `OK` |
| removed channel | `OK` | `OK` |
| added `NotRequired` channel, node indexes it | — | `KeyError: 'extra'` |
| added `NotRequired` channel, node uses `.get()` | — | `OK` |

Three findings. **The failure is the added name, not the removed one** — a rename is both, and it is
the addition half that raises, because nothing indexes a channel that no longer exists. **At a turn
boundary the checkpoint is not what fails**: the run starts at `START`, so a node indexing an
unwritten channel fails identically on a brand-new thread, which means the shipped repro measured a
plain bug rather than a migration failure. The stranding is specific to a run resumed *inside* the
graph, where the node that writes the channel does not re-run. And **`NotRequired` is not a usable
filter**: it constrains how the input may be spelled, not whether a node indexes the channel.

## Decision

**1. The stamp is the channel names *this repository* declares.** `FIRST_PARTY_CHANNELS` is
`ChemclawState`'s channels minus those of the base it extends, recovered through `__orig_bases__`
because the merged `__annotations__` cannot answer the question. Measured: `('loop_capped',
'model_calls')`, down from six. An upstream channel that moves cannot move the stamp, so a
dependency bump cannot refuse a thread. Middleware channels stay outside it for the reason the prior
ADR gave — `create_agent` merges those in, and this module cannot see them without importing the
agent builder that imports it.

`__orig_bases__` is only populated while the base is generic (`PlanningState` extends
`AgentState[ResponseT]`), so the subtraction could silently become a no-op. A test asserts the
derived set stays **disjoint** from the upstream base's channels, which turns that into a red build
rather than a fleet-wide refusal.

**2. The check is asymmetric, matching the measurement.** The stamp is stored as the list of names
and a checkpoint is refused only when this build declares a channel the stamp does **not** hold. A
channel this build has *dropped* leaves extra names in the stamp and resumes — a deploy that only
deletes a field now ends no sessions, where fingerprint equality would have ended all of them for a
change measured harmless.

**3. The refusal is still wider than the failure in one place, and that is stated rather than
implied.** An added channel is refused even when every reader uses `.get()` and the resume would
have worked, because the stamp holds names and cannot see how a node reads one. That over-refusal
lands only on a state change this repository is itself deploying — which it can drain sessions for —
never on a dependency's. Also uncaught, unchanged from the prior ADR: a same-name type change.

**4. A new metadata key, `chemclaw_state_channels`.** The value changed shape (a hash then, a list
now) and a rolling deploy runs both builds at once. Under one key each build would read the other's
value as a mismatch and refuse the thread; under two, each reads the other's checkpoints as
unstamped and resumes them. Anything that is not a list of names is treated as no stamp at all.

## What was rejected

- **Keeping the fingerprint and living with the dependency coupling.** It is the guard's own worst
  failure: fleet-wide, on a change nobody reviewed as a state change, with a refusal message
  naming two opaque hashes.
- **Refusing on any difference to the first-party set.** Simplest, and it ends in-flight sessions on
  a pure deletion, which is measured to resume cleanly. A guard's false positives are sessions.
- **Narrowing the stamp to *required* channels**, on the theory that `NotRequired` means a node
  tolerates absence. Measured false: an added `NotRequired` channel a resumed node indexes raises
  `KeyError` just the same. It would also have made the stamp empty today, since both of this
  repository's channels are `NotRequired`, leaving a guard that guards nothing.
- **Keeping the hash and storing the names beside it.** Two representations of one fact, one of
  which nothing reads. The names are what make the refusal message name the missing channel.

## Consequences

- The refusal now says which channels the thread never held and which ones it does hold, instead of
  two twelve-character hashes.
- `tests/test_checkpointer_schema.py` measures the failure *with its control* — the same build
  answering on a fresh thread — so the claim that the checkpoint is what strands the turn is on the
  record rather than assumed; and it measures the removal, the `NotRequired` pair, the upstream
  exclusion (as the dependency bump it is meant to survive), the legacy hash stamp, and the
  pre-guard unstamped checkpoint.
- Any environment that ran #173 has checkpoints stamped under the old key. They resume, and their
  next write stamps them under the new one.
