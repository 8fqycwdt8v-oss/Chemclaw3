# D-2026-09-05-eight-territories-over-one-review — what reproduction-before-fix found, and what it unfound

## Context

`tasks/review-2026-09-04-tool-integration-and-storage.md` recorded 14 HIGH and 28 MED across the
tool-integration seam, the middleware chain, the calculation cache, the publish path, retrieval, the
`Chemclaw3-mcp` fleet, and what the suite can fail. This ADR records what happened when it was
worked, because two of the more useful outcomes were findings that did not survive contact.

Eight fixers on **strictly disjoint file territories**, each required to reproduce at `HEAD` before
touching anything and to write the failing test first. Fixers ran only their own targeted tests: the
verification sweep had measured this box OOM-killing at 12.4 GB anon-rss with three concurrent
suites in a 16 GB cgroup, so the coordinator ran the full gate serially at the end.

## What the method produced

**Two findings did not reproduce, and one of them was the coordinator's.**

`H7` claimed `agent_max_parallel_tool_calls` (8) × `agent_max_tool_result_chars` (60,000) put
120,040 tokens in a batch compaction may not clear. `agent/tool_result_size.py` **divides** the
ceiling across a batch — eight calls get 7,500 characters each and the batch totals 60,000, i.e.
15,040 tokens, and the floor at `HEAD` was 57,770 against a 100,000 budget. The reviewer's own
report had said "60,000 divided by `batch_width`" and the coordinator did not apply it to the
product. The *conclusion* held for the other reason (`H8`, one layer out), so `H7` and `H8` were
never two findings — which is `D-2026-08-01-a-cap-that-starves-a-source` happening to the reviewer
rather than the reviewed: when two explanations compete, the articulate one is uncorrelated with the
true one.

`M9b` claimed text and boolean facts skip the registry check numeric facts get at enqueue. They do
not: `PropertyFact._belongs_in_the_scalar_table` calls `definition_for` on every fact whatever its
value kind, so construction raises inside `records_for`. A regression test was added, because the
guard is a side effect of a validator written for a different question and nothing said so.

**One prescription was measured and rejected.** The review proposed bounding vapour pressure at
1.5 × Tb. At 1.5 water's ceiling is 286.6 °C, which refuses **300 °C water** — a question `props`
already answers against the steam tables to +14%. A bound that refuses a question the correlation
answers usefully is worse than the runaway it was added to stop. Shipped at 1.8 with the cost stated
(~404 bar still admitted, which is supercritical) and *why not 1.5* encoded as a test.

**Three fixes were declined with reasons.** The artifact-eviction defaults stay off, because turning
them on makes an *upgrade* delete a chemist's Hessians and the byte budget is a per-deployment fact
no default can know; what changed is the unreachable growth, reclaimed at the rewrite. The SVG size
reduction was declined at 17,188 of 34,526 characters, because a `<style>` element inside an inline
SVG is document-scoped in HTML and two depictions on one chat page would restyle each other. And
"a bearer bundle whose token variable is unset is `unusable`" was **built, measured and reverted**:
no token is mounted in an ordinary test, CLI, worker or template-activity process, so every shipped
bearer bundle became unusable at once and `connector_specs()` returned `[]`. That is a change to
what a tokenless process *is*, not a bug fix; the reasoning is recorded in `unusable_reason`'s
docstring so the gap is stated rather than rediscovered.

**One finding was worse than reported.** `H9` said a second chemist's provenance was dropped. The
primitive publish hook passed no `Publication` at all, so the actor index held no row for **any**
primitive this system had ever computed — see
`D-2026-09-05-an-outbox-row-is-a-record-and-its-publications`.

## The pattern the review named, and what working it added

The review's dominant pattern was *a control applied at every site but one*, six instances. Working
it found the seventh and eighth in the tree's own test apparatus rather than in `src/`:
`tests/conftest.py` cleared three `@cache`d discovery registries under a docstring arguing that
"remember to clear the cache" must become an invariant nothing can forget — and did not cover
`core.tool_registry`, which `_register_generated_tools` populates at call time. A test written to
prove the collision guard works on a process's *first* agent build passed alone and failed in the
full suite, on its own precondition assertion, correctly refusing to be evidence while something
upstream had already registered the launcher. The fixture now snapshots and restores.

And `infra/sql/grants/app_privileges.sql` needed a row for the new table in **both** directions:
the gate caught the missing INSERT, then caught an UPDATE nobody uses.

## Consequences

Behaviour changes a deployment will feel are in the individual ADRs. The method note worth keeping:
requiring reproduction-before-fix paid for itself four times in a pass of eight, and every one of
those four was a case where the more articulate explanation was the wrong one.
