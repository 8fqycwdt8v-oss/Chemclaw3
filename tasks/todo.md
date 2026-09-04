# Backlog analysis — 2026-09-04 (session 2)

Selected from `docs/planning/BACKLOG.md` after re-reading all 44 open rows. Criterion: closable
here, with evidence, this session. The environment's `API-KEY` was probed first (one Haiku call,
200) because three rows are blocked exactly while it is down.

## Selected

- [x] **Half the probe corpus tests one tool** (§5) — wire and *run* the tool-utility A/B arm over
      bucket A with the cheapest model (Haiku 4.5). Blocked-on-credential row; the credential answers
      today, so the measurement is owed today.
- [x] **`core/fulltext.py`'s tokeniser can revert to the exact bug its own comment names** (§3) —
      three surviving mutants get tests that kill them; the three modules join `[tool.mutmut]`.
- [x] **`tests/pg.py`'s `TEST_SCHEMA` recycles pids** (§3).
- [x] **33 rendered-chart tests are gated on an unpinned `helm` the `check` job never installs** (§3).

## Deliberately not taken

- **A stalled append-only feed has no first-party signal** — its own trigger (a deployment running an
  `append_only:` source) is not met; no shipped binding sets it.
- **`delete_session` and the owner prune take two rows in opposite orders** — the row records a fix
  already tried and rejected; it is a note, not work.
- **Memory records; it does not change what the next turn does** — blocked on deployment history,
  and the measurement it wanted is already a shipped command.

## Review

**What shipped.** Four rows closed or halved, and two defects found by trying to do the work.

1. **The tool-utility A/B exists and has been run** — `make live-ab`, a control profile with
   `tool_names: []`, 221 probes, 442 turns, ~29 minutes, `claude-haiku-4-5-20251001` on both arms.
   ChemToolAgent's finding reproduces on this corpus: bucket A tools **helped 31%, hurt 23%**, and
   **19** questions the toolless model correctly declined came back fabricated. Bucket C came out
   the other way and falsified the hypothesis it was built on. A tool-armed turn costs **30.6x** a
   toolless one. `D-2026-09-04-tools-help-a-third-of-the-time-and-hurt-a-quarter` +
   `docs/archive/tool-utility-2026-09-04.md`.
2. **Three mutants killed** — `core/fulltext.py`'s `_WORD`, `templates/resolve._WHOLE`'s anchors,
   and a `_NOT_A_QUANTITY` test whose fixture never reached the mechanism it was named after. Each
   mutation was applied, run, observed failing, and reverted; the three modules joined
   `[tool.mutmut] source_paths`.
3. **`TEST_SCHEMA` is a uuid** rather than a recyclable pid, and **`helm` is installed in CI's
   `check` job** behind a single `HELM_VERSION` pin both jobs read, with a skip epilogue in the
   shape `tests/pg.py` already had — so 33 rendered-chart tests stop skipping silently.

**Two defects the work uncovered, both fixed here.** `infra/live/processes.sh` hardcoded
`chem safety` as its fleet bundles, so the `rxnpredict` bundle wired earlier the same day failed
the front door on every `make live-up` — the list is now derived from the two manifests that
already declare the set. And `evals/live_judge.py` honoured `llm_base_url` while ignoring
`llm_tls_ca_bundle`, so grading died at TLS against exactly the internal gateway that setting
exists for; it now reuses the agent's own `_tls_http_client`.

**What was not taken, and why it is not a gap.** `plan_execute_utility` still scores four invented
floats. Folding tool-utility data into a metric named for *planning* would trade an honest gap for
a mislabelled number, so the eval-gate row stays open with one fewer excuse.

**The one number here that is an estimate rather than a measurement** is the judge's share of the
bill: `live_judge.py` counts its own usage nowhere, so ~$5 of the run's ~$13 is inferred from
prompt sizes. The front door's half is exact, from its own per-profile counters.
