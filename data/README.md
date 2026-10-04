# `data/` — the corpora and datasets the code reads at runtime

Everything here is **data, not code**: no `.py`, nothing importable. Each directory is pointed at by
a `CHEMCLAW_*` setting, so a deployment can mount its own without rebuilding the image.

| Directory | What it is | Setting |
| --- | --- | --- |
| `evals/` | the versioned case-set, the retrieval corpus, the committed baseline, the live probes and the vendored external benchmark | `CHEMCLAW_EVAL_CASE_DIR`, `CHEMCLAW_EVAL_BASELINE_PATH`, `CHEMCLAW_EVAL_RETRIEVAL_CORPUS_DIR`, `CHEMCLAW_LIVE_PROBE_DIR`, `CHEMCLAW_BENCHMARK_DIR` |
| `templates/` | the shipped step-template YAML | `CHEMCLAW_TEMPLATES_DIR` |
| `profiles/` | agent profiles selecting across skills and tools | `CHEMCLAW_PROFILES_DIR` |
| `vendored/` | build-time datasets, with their provenance | `CHEMCLAW_VENDORED_DATASET_DIR` |
| `eln-exports/` | sample ELN drops for the JSON and ORD adapters | `CHEMCLAW_ELN_EXPORT_DIR` |
| `commitments/` | a programme's committed work, mirrored in by the `commitments-json` source | `CHEMCLAW_COMMITMENT_EXPORT_DIR` |

`evals/` and `templates/` are here rather than at the root so they do not share a name with the
code packages `chemclaw.evals` and `chemclaw.templates` (D-156).

**Two runtime directories are deliberately *not* here.** `knowledge/` and `skills/` stay at the
repository root because they are architecture layers 4 and 3, not configuration: they are what the
system knows and how it judges, and they are authored by people. Burying them one level down to
make a rule exceptionless would trade a real distinction for a tidier sentence.

## `vendored/` carries provenance or it does not load

A shipped corpus is the one sanctioned escalation of D-089's "no external sources" rule, and it
earns that by being **local** — installed at build time, read from disk, never fetched. Its
manifest must declare `name`, `version`, `licence`, `retrieved_from`, `sha256` and whether it is
`mirrored` (see `vendored/README.md` for the full set); the schema requires them, so the pull-request review that approves a dataset is about something identifiable
later. `tests/test_no_egress.py` also forbids the reader from importing an HTTP client at all, so it
cannot acquire one in a later edit.
