# The four things the constraint-rot audit left open

Follow-up to `D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision` (#406, merged).

- [ ] **The forkserver ratchet reds under memory pressure and its docstring says it cannot.**
      117.65/117.59 MiB under load, 109.0 quiet, closure unchanged, CI green. Either `VmRSS` is
      load-sensitive here — in which case the unit is wrong for a second time — or something in the
      fork path is. Root-cause by measurement; do not raise `FORKSERVER_RSS_CEILING_MIB`, three
      derived values sit under it.
- [ ] **D-092 splits rather than reopens.** Its condition *was* met — by `Chemclaw3-mcp`'s
      SHA-pinned build-time weight bakes, not by `D-135` — and the answer is still no, on three
      different grounds: MACE-OFF's weights are non-commercial, ANI-2x/AIMNet2 are blocked on
      demand rather than vendoring, and retrosynthesis was never governed by that condition.
      One ADR, `Revisit when:` on every declining half, at least one trigger executable.
- [ ] **A fourth dead vocabulary.** The removed agent framework is the largest unmarked one after
      the PR-gate. Marker section plus a `tests/test_dead_vocabulary.py` entry, same shape as the
      three that ship.
- [ ] **The arrears.** 231 ADRs that no index says anything about. Triage rather than bulk-file:
      the table's job is "where the current decision on a subject is", and most of these are defect
      write-ups, which the new ADR rule says should not have been ADRs. File what is a subject,
      declare the rest in `_NOT_A_TOPIC` with a real reason, lower the allowance to what is
      measured.

## Not done, and why

- **Deleting the two merged branches.** `git push --delete` fails with `the remote end hung up`
  across four forms (`--delete`, `:refspec`, HTTP/1.1, repeated retries) in both repos, while the
  agent proxy reports healthy with no relay failures — so it is the zero-object delete push
  specifically. No MCP tool deletes a branch. Needs the GitHub UI.

## Verification

- [ ] `make lint type test` green; each new ratchet driven to red before it is believed.
