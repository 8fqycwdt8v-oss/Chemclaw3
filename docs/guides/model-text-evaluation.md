# Evaluating a batch of model-facing text

Everything a model reads is a prompt: tool docstrings and argument descriptions, prompt blocks,
`SKILL.md` files, the fleet's tool descriptions. A batch of edits to it ships only when the
evaluation below says it is not worse than the shipped text
([the decision](../decisions/D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation.md),
refined by [this one](../decisions/D-2026-10-08-the-text-evaluation-states-its-power-and-holds-the-prefix-to-its-claim.md)).
This guide is the protocol, and `make model-text-eval` is the protocol as code.

## 1. Know what you are editing: the inventory

```
make model-text            # writes schema/model-text/inventory.json
```

The inventory lists every string a model reads: owner (`file:symbol`), kind, tokens, characters, a
content hash, and where it is paid — `every-request` (the default profile's prefix), `conditional`
(a profile, an unbound tool) or `on-demand` (a skill body, a tool result, another model call). Its
`prefix` section is the per-request prefix the evaluation compares, and `environment` records the
settings that decided what was measured. `schema/model-text/README.md` says how it relates to the
context floor.

`tests/test_model_text_inventory.py` fails when the committed file is stale, so **every edit to
model-facing text puts an inventory diff in its pull request**. That diff is the tripwire: if you
see one, the batch needs the evaluation below.

The inventory is measured under the image's Python (3.11, `[tool.mypy] python_version`); run it as
`uv run --python 3.11 …` on a machine with another interpreter. It refuses to be written while
`CHEMCLAW_MODEL_TEXT_OVERLAY_DIR` is set, because that would commit candidate text as the shipped
text's inventory; a candidate's goes elsewhere with `--output`, and records the overlay's digest.

## 2. The rule

A batch is one tool family (one fleet server, or one core tools module with its schema classes);
prompt blocks and skill bodies come last, one block at a time. It ships only if:

- **no metric is worse than the control by more than the control's own spread**, for each of the six
  metrics below,
- **the per-request prefix has not grown past a tolerance** (100 tokens, `--prefix-tolerance`) and,
  if the batch **claims a token saving** (`--claims-token-saving`, off by default), **has shrunk**,
- **each arm completed the probes** (see coverage below), and
- **there are at least 5 runs per arm** (`--min-runs`, which can be raised and not lowered).

| metric | better | measured as |
| --- | --- | --- |
| tool selection accuracy | higher | share of probes with an applicable `expects_tools` that called one of them |
| first-call argument validity | higher | `1 - errors / first calls`, pooled over every tool's first call in the run; an error is a schema failure on that first call |
| refusal correctness | higher | share of graded bucket-C probes the judge calls `served` (an honest refusal, not a fabrication) |
| graded task success | higher | mean judge score (served 1, partial 0.5, unserved 0, fabricated -1) over graded bucket A and B probes |
| tokens per turn | lower | mean tokens (input, output, both cache counters) booked in `turn_costs` per probe |
| turn cost | lower | mean billed token-equivalents (cache read and write weighted as `eval_cache_*_weight`) per probe |

A run in which a metric has nothing to measure (no refusal probe selected, no ledger row) reports
`None` for it; a metric with no measured run in an arm is **unmeasured**, which blocks shipping.

### The statistics, and what they can and cannot see

- **Noise floor** = the control arm's range over its runs, `max - min`. Not a standard deviation:
  a few runs cannot estimate one, and the range is the spread actually observed.
- **Worse by** = the difference of the arm means, signed so that positive is worse.
- **A metric passes** when `worse by <= noise floor`. Exactly the spread passes ("more than").
- **A control that never varied** (spread 0) leaves no room: any worsening above float error fails.
- **An improvement is never a reason to refuse**, however large.

**Power.** This rule was simulated on normal noise (`rule_power`, reproducible; the figures below
are its output and a test pins them). σ is one metric's run-to-run standard deviation.

| runs per arm | neutral edit fails some metric of six | 1σ regression passes | 2σ regression passes |
| ---: | ---: | ---: | ---: |
| 3 | 36% | 71% | 39% |
| 5 | 5% | 89% | 60% |
| 7 | 1% | 97% | 76% |

More runs remove false failures and **do not** make regressions easier to catch: the range widens
with the run count. So the rule is a guard against regressions larger than the noise; one the size
of the noise floor passes about half the time, a smaller one passes. Every report prints this for its
own run count, and per metric the floor in that metric's units (`σ≈…`). Five runs is the default and
the floor for a ship: the first count at which a neutral rewrite is not likely to fail on noise.

**Coverage.** Both arms are compared on the probes every run of both completed. A probe the harness
failed on (no answer reached the front door, the judge did not grade it, the ledger did not book
it) is dropped from the comparison in both arms, and the report states how many each arm dropped.
A turn that answered nothing is not dropped: it is a result. If either arm dropped more than 10% of
the probes (`--max-drop-share`) the comparison is refused.

## 3. Build the candidate

Two ways, depending on what the batch edits.

**An overlay**, for tool descriptions and prompt blocks, needs no edit to the repository. One file
per text, in a directory of your choice:

```
overlay/
  tools/<tool name>.txt     replaces that tool's description (first-party or connector)
  blocks/<index>.txt        replaces _INSTRUCTION_BLOCKS[<index>]
  safety/<index>.txt        replaces _SAFETY_BLOCKS[<index>]
```

The front door started with `CHEMCLAW_MODEL_TEXT_OVERLAY_DIR=<overlay>` runs that text and logs a
warning with the overlay's digest. It refuses to start if a file names a tool or block that does not
exist, sits outside the layout, or is empty, so a misspelt or blank file cannot make the candidate a
second control or a prompt with a hole in it. A block keeps the shipped block's leading and trailing
whitespace.

**A checkout**, for anything the overlay cannot express — above all argument descriptions in a
schema, which live in code. Make the edits on a branch, run the candidate front door from it, and
pass its `schema/model-text/inventory.json` as `--candidate-inventory`; it must have been measured
in the same environment as the control (same `.env`, same connector set), which the evaluation
checks against the `environment` each inventory records.

## 4. Run the evaluation

You need a gateway credential: `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_LLM_MODEL` and
`CHEMCLAW_LLM_API_KEY`, in the environment of this command (the judge runs here) and of both front
doors. Without them the command exits 3 and names the three settings, with or without other
arguments. There is no way around it, and a batch does not ship without a live run.

Start the live stack (`make live-up`) for the control, then a second front door for the candidate
with the same environment, another port and the overlay:

```
CHEMCLAW_MODEL_TEXT_OVERLAY_DIR=/path/to/overlay \
  uv run uvicorn chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8001
```

Then:

```
make model-text-eval ARGS="--candidate-url http://127.0.0.1:8001 --candidate-overlay /path/to/overlay"
```

The first run prints the plan and stops: **a live run starts only when the planned count is typed
back.** By default it asks 50 probes (a systematic sample; `--sample 0` asks all of them), 5 runs,
2 arms: 500 agent turns and up to 500 judge calls. Add `--confirm-turns 500` to spend it. A plan
over `--max-turns` (500) is refused until `--max-turns` is raised on purpose.

What it does, in order, stopping at the first refusal:

1. **Credential check** (exit 3 if missing), then the plan above.
2. **Offline gate**: `make eval-strict`, `make eval-baseline-check` and
   `tests/test_prose_contract.py`, all of them even if one fails. These run on the working tree, so
   for an overlay they confirm the repository, not the candidate; a checkout candidate runs them in
   its own checkout.
3. **The prefix**: both arms are measured the same way, by running `model_text_inventory` in a
   process with this command's environment, differing only in the overlay (set for the candidate,
   emptied for the control). Two prefixes measured in different environments are refused, because a
   difference between them could be the environment's.
4. **The arm check**: each front door's `/readyz` must report what its arm is labelled with. A
   door running an overlay adds `model_text_overlay` (the overlay's digest) to its body; the control
   must report none and the candidate the digest of `--candidate-overlay`. A candidate started
   without its overlay is a second control and would read "no effect" for any text, so the run is
   refused (exit 2) before a probe is spent. A candidate from a checkout reports no overlay, and
   its identity is the operator's to get right.
5. **The live runs**: every selected probe, asked of each front door, graded by the judge, costed
   from the ledger. Control and candidate alternate, run by run, so gateway drift reaches both
   arms. The control's runs are the noise floor.
6. **The decision**, the table below, and `summary.md` / `results.json` in the run directory.

| exit | meaning |
| --- | --- |
| 10 | **ship**: a live run, offline gate passed, every rule met. Nothing else returns it |
| 0 | `--offline-only`: the gate passed and nothing was shipped |
| 1 | no ship: a metric is worse, the prefix grew, the probes were lopsided, or the offline gate failed |
| 2 | undecided: too few runs, a run the judge graded nothing of, a refusal to spend, a misuse, or any `--dry-run` |
| 3 | unreached: no gateway credential, or no probe reached a front door |
| 4 | a live table that would ship, with `--skip-offline`: not a ship, and the output and `results.json` say the gate was skipped |

### Dry run

```
make model-text-eval ARGS="--dry-run"
```

drives the same pipeline with deterministic fake answers, grades and spend (`--dry-run-shift -0.3`
makes the candidate worse, `0.3` better, `--dry-run-drop 0.3` fails a share of the candidate's
grades). Every line it prints and writes is labelled **NOT EVIDENCE**, and it exits 2 whatever it
decided, so it cannot be mistaken for a result. Its plumbing is covered by unit tests
(`tests/test_model_text_eval.py`); no CI job runs it. Never paste its table into a pull request.

## 5. The table a pull request carries

```
Live evaluation: gateway `<url>`, model `<model>`, judge `<judge>`; 5 runs per arm over 50 probes, control and candidate alternating.

| metric | better | control mean (spread) | candidate mean (spread) | worse by | noise floor | verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| tool selection accuracy | higher | 0.833 (0.058) | 0.840 (0.077) | -0.00641 | 0.05769 | pass |
| first-call argument validity | higher | 0.934 (0.038) | 0.960 (0.016) | -0.02569 | 0.03758 | pass |
| refusal correctness | higher | 0.619 (0.143) | 0.667 (0.143) | -0.04762 | 0.1429 | pass |
| graded task success | higher | 0.676 (0.066) | 0.626 (0.151) | +0.05031 | 0.06604 | pass |
| tokens per turn | lower | 9,896 (195) | 9,886 (136) | -10.12 | 195 | pass |
| turn cost | lower | 10,886 (214) | 10,874 (149) | -11.13 | 214.4 | pass |
| per-request prefix (tokens) | lower | 71,839 | 71,739 | -100 | +100 allowed | pass |
| probes dropped (share) | lower | 0 (0.0%) | 1 (2.0%) | — | ≤ 10% | pass |

Runs per arm: 5 (a ship verdict needs 5). With 5 runs per arm this rule fails a neutral edit on a given metric 1% of the time (5% on some metric of six) and catches a true regression of 1σ 11%, 1.5σ 23%, 2σ 40% (σ is one metric's run-to-run standard deviation). A regression smaller than the noise floor is not detectable.
Prefix: smaller by 100 tokens.
Coverage: compared on the 49 of 50 probes both arms completed.
Power, tool selection accuracy: σ≈0.0248: a regression of 1σ passes 89%, of 2σ 60%.

**SHIP** — no metric is worse than the control's own spread and the prefix is held

Offline gate:
- pass `eval-strict`
- pass `eval-baseline-check`
- pass `prose-contract`
```

Mean and spread (range) per arm, `worse by` (positive is worse), the control's noise floor, a verdict
per row, the power for this run count, and the overall verdict. The numbers above are illustrative
of the format only; they are not a result. The command prints this table (with the evidence line
first) and writes it to `summary.md`.

## 6. After the batch ships

- Lower `CEILINGS` in `tests/test_context_floor.py` to the measured prefix (programme item W2.16), so
  the saving goes to the thread budget.
- Commit the regenerated inventory: it is already in the diff, because the staleness test made you.
- Put the table in the pull request and a one-line summary in `tasks/todo.md` (W2.17).
- A fleet batch bumps that connector's `contract_version` minor.

## Limits of what this measures

- The judge is a model; its noise is inside every metric's spread, and a judge swap invalidates a
  comparison made with the old one.
- Argument validity is recognised from the failure text (`evals/live.ARGUMENT_ERROR`), pinned by
  `tests/test_live_argument_validity.py` against the two real producers.
- A first call is attributed to a tool only while exactly one call to it is outstanding, so a
  parallel batch of calls to one tool is not counted.
- The corpus is the live probe set, sampled; a text that matters on a question it does not ask is
  outside it.
- The range floor does not detect a regression smaller than the noise, at any run count.
