# D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose — The count lives in the test, not in the prose

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** the last two Block H rows (wrong
counts, retired concepts) · **Extends:**
D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check (the mechanical half)

## Context

Widening `prose-validate` fixed everything a resolver can check: paths, ADR ids, config keys. It
explicitly left two classes it cannot, and this ADR is those two.

**Eight counts in prose are wrong**, every one of them. Verified against code rather than taken from
the row that reported them — which itself claimed nine, and was wrong about one:

| Prose | Says | Is | |
|---|---|---|---|
| `README.md` | "the three plain secrets" | six env keys plus Temporal mTLS as files | |
| `README.md` | "the two Temporal workers" | four Deployments: background, plus `calc`/`bo`/`qm` | |
| `CLAUDE.md` | "three-secret model" | six | |
| `CLAUDE.md` | "All 167 numbered ADRs" | 165 (`D-001`…`D-124`, `D-130`…`D-170`) | |
| `deploy/README.md` | "Five exist" | six — omits `auditAnchorSecret` | |
| `architektur.md` | "Nur drei Klartext-Secrets" | six | |
| `runbook.md` | "Six bundles" | seven — `qm` is missing entirely | |
| `runbook.md` | "`bo` — the one that also owns durable work" | `calc`, `bo` and `qm` each declare `jobs:` | |
| `ARCHITECTURE.md` | "the eight validators `make` runs" | six live in `cli/`; `make` runs nine | |
| `tests/test_helm_chart.py` | "no sixth crept in" | six exist, so the next is a seventh | |

The reported ninth was `values.yaml`'s "so a seventh cannot arrive unnoticed", called an off-by-one
in the opposite direction. It is **correct** — there are six, so a seventh is exactly what the test
guards against. Checking it was worth the minute: the fix would have introduced the error.

`deploy/README.md` is the sharpest case. It enumerates five secrets and then, two sentences later,
says the count "is now derived from `values.yaml`'s `secrets.keys` rather than restated here,
because a number in prose is exactly what went stale". It restates it in the previous sentence.

**Six retired concepts are still asserted as current**, most of them contradicted elsewhere in the
same file or by a sibling document that is right.

## Decision

**Delete the number; let a test assert it.** This is not a new position — it is the one this
repository has already reached twice and written down, and the failure was not adopting it
everywhere. `values.yaml` says it outright: *"This comment used to say 'the THREE documented plain
secrets' and went stale twice without anyone noticing, which is why the count now lives in the test
rather than in prose."* `CLAUDE.md` says the same about its own command list: *"a count is not
written here, because the one that was said 23 while the file held 28."*

So prose says **what the set is and where it is pinned**, never how many. "The plain secrets (the
set `values.yaml` declares and `tests/test_helm_chart.py` pins)" cannot go stale, because the only
thing that could falsify it is the pin disappearing — and that is a test failure.

**Where a count genuinely aids a reader, it becomes an assertion first.** Two counts were worth
keeping as facts rather than dropping: the bundle set and the set of bundles owning durable work.
Both are now derived in `tests/test_repo_map.py` from `connector.yaml` files on disk, so the runbook
can name them and be wrong only if a test fails.

**A count in a docstring is prose too.** `tests/test_helm_chart.py`'s "no sixth crept in" sits three
lines above an assertion listing six keys. The assertion is right and the sentence describing it is
wrong, which is the exact shape that makes a stale count survive review: it reads as corroboration.

**Retired concepts are corrected to what the code does, and the correction names the ADR that
retired them** — so the next reader gets the history rather than a bare replacement:

- **Task queues.** `architektur.md` and `CLAUDE.md` both say two core queues, `hpc-jobs` and
  `background-jobs`. `core/config.py` states the truth in its own comment: *"There is no second core
  queue any more — the heavy `hpc-jobs` queue went with the QM job into `connectors/qm/`."* It is
  `background-jobs` plus one derived `connector-<name>` queue per bundle that owns durable work
  (D-118/D-150). `ARCHITECTURE.md` already had this right, so the tree contained both answers.
- **"MCP server" as a deployable.** `architektur.md` §6 lists an MCP-Server deployment role.
  `deploy/entrypoint.sh` dispatches `service`, `background-worker`, `connector-worker-*` and
  `connector-*` — nothing else. `deploy/README.md` deleted this exact row under D-156 as "prose
  asserting a deployable that does not exist"; the same sentence survived here.
- **"MCP servers hold capability"** (`CLAUDE.md`). `ARCHITECTURE.md` says connectors do, which is
  what D-110/D-118 built. MCP is the protocol a connector speaks, not the thing that holds the
  capability.
- **"HPC/DFT is deferred"** (`CLAUDE.md`), 58 lines after the same file says F5 shipped the real
  Nextflow launcher. `DEFERRED.md` resolves it and the fix adopts that resolution: the launcher is
  built, the *cluster* is what is missing.
- **`actor` is a Phase-6 seam, `'unknown'` until Entra is wired** (`infra/sql/006`). It has carried
  the turn's Entra `oid` since F4 — `agent/audit.py` reads `get_current_actor()`. A comment on the
  audit table saying its actor column is unpopulated is the worst place in the system for a stale
  sentence.
- **`architektur.md` §8 on role-scoped skills, which is wrong in the opposite direction.** It says
  skill filtering by role "muss erweitert werden". `agent/skill_access.py::RoleScopedSkillsSource`
  has done it since D-052. The backlog row that reported this class described it as prose
  *overstating* the code; here the prose understates it, and a reader trusting the document would
  rebuild something that exists.

Two more corrections of the same kind, caught by the verification pass but not by any resolver:
`runbook.md` names the metric `chemclaw_tool_latency_seconds` (it is
`chemclaw_tool_duration_seconds`) and the invocation `chemclaw explain <session-id>` (the console
script has no subcommands; it is `make explain SESSION=<id>`, D-166).

## Why not the alternatives

**Teach `prose-validate` to check counts.** A number in prose has no syntactic marker — "three",
"3", "the third" — and no authority to resolve against without knowing which set is meant. Every
version of this rule is a heuristic, and a heuristic in a gate is argued with rather than obeyed.
Deleting the number removes the checkable claim instead of trying to check it.

**Keep the counts and add tests that assert the prose.** A test parsing English for numerals, then
mapping each to the right set, is a second parser of the thing the first parser cannot do. It also
fails in the direction that wastes the most time: a rewording breaks CI without anything being
wrong.

**A deny-list of retired terms** (`hpc-jobs`, "MCP-Server", `services/chemclaw/`) in the validator.
Tempting, and it would have caught four of the six. It is a curated list rather than a derivation,
so it carries the review friction of `_ALLOWED_NON_TOOLS` without its justification — and a term
retires roughly once per architectural decision, which is rarely enough that the ADR is the better
record. Reconsider if the class recurs.

**Leave `architektur.md` §8 alone because it is historical.** `docs/README.md` marks `reference/` as
*partly* maintained and says to read it for intent. That is a caveat about detail, not a licence for
a paragraph telling a reader to build something that exists.

## Consequences

- Eight counts are gone from prose. Two survive as facts because a test now derives them; the rest
  are replaced by a pointer to the set and its pin.
- `tests/test_repo_map.py` asserts the bundle set and the durable-work set from `connector.yaml`
  files, so the runbook's description of what ships is checked rather than remembered.
- Six retired concepts are corrected, each citing the ADR that retired it. Two documents that
  disagreed with themselves (`CLAUDE.md` on HPC, `runbook.md` on which bundles own durable work) now
  agree.
- **Block H is complete, and with it the A–H programme.** What remains of the doc drift is one
  filed row: `docs/planning/` fails the mechanical rules 175 times, and each needs rewording rather
  than a path substitution.
- The `values.yaml`/`CLAUDE.md` position — a count belongs in a test — is now applied uniformly
  rather than in the two places that had already been burned.
