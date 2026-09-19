# The four things the constraint-rot audit left open

Follow-up to `D-2026-09-19-a-refusal-that-cannot-expire-is-not-a-decision` (#406, merged).

- [x] **The forkserver ratchet reds under memory pressure and its docstring says it cannot.**
      117.65/117.59 MiB under load, 109.0 quiet, closure unchanged, CI green. Either `VmRSS` is
      load-sensitive here — in which case the unit is wrong for a second time — or something in the
      fork path is. Root-cause by measurement; do not raise `FORKSERVER_RSS_CEILING_MIB`, three
      derived values sit under it.
- [x] **D-092 splits rather than reopens.** Its condition *was* met — by `Chemclaw3-mcp`'s
      SHA-pinned build-time weight bakes, not by `D-135` — and the answer is still no, on three
      different grounds: MACE-OFF's weights are non-commercial, ANI-2x/AIMNet2 are blocked on
      demand rather than vendoring, and retrosynthesis was never governed by that condition.
      One ADR, `Revisit when:` on every declining half, at least one trigger executable.
- [x] **A fourth dead vocabulary.** The removed agent framework is the largest unmarked one after
      the PR-gate. Marker section plus a `tests/test_dead_vocabulary.py` entry, same shape as the
      three that ship.
- [x] **The arrears.** 231 ADRs that no index says anything about. Triage rather than bulk-file:
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

## Review

**Three of the four were corrections to claims I had made hours earlier**, which is the pattern
worth keeping rather than the four deliverables.

- The forkserver was **not** load-sensitive. I told the user it was, from four readings. Driven
  properly it is 109 readings over eleven arms inside a 0.45% spread, `VmRSS == VmHWM` every time,
  and all four candidate mechanisms falsified. What moves it is the *environment*: `forkserver`
  execs, so `site` runs the venv's `a1_coverage.pth` and `coverage` lands in the measured process —
  +5.0 MiB, through a 3 MiB margin. The 117.6 pair is still unexplained and is recorded as
  unexplained.
- D-092's condition **was** met, in the sibling fleet, and the ADR I merged said both bakes were
  pinned and allowlisted. Only `rxnpredict`'s is.
- The arrears were **230, not 231**: one ADR the table already cited counted as unfiled because the
  citation carried a literal ellipsis the resolver cannot match.

**What I got wrong procedurally**: I committed an agent's in-flight file because a hook flagged it
and the tests were green. Green is not finished — it captured a draft with two wrong figures, and
the correction is its own commit rather than a rewritten one, because the wrong version was already
pushed.

**Left open deliberately**: the two merged branches cannot be deleted from here (four push forms,
both repos, proxy healthy); one forkserver sample in ~150 has never been reproduced and is recorded
in the constant's comment rather than smoothed away.
