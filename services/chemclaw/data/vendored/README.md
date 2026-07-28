# Vendored reference data

Datasets installed into the container image **at build time**, read from local disk at runtime.
This is the one sanctioned escalation of D-089 ("no external data sources"), and it is narrow by
construction: a corpus arrives the way a dependency arrives — pinned to a version, checksummed,
labelled with its licence, and reviewed once by a person in a pull request. Nothing here is fetched
while the system runs, and `tests/test_no_egress.py` asserts that the source module holds no host
literal and imports no HTTP client.

## What is here today

`common-reagents` v0.1.0 — reagents, solvents, bases, ligands and coupling reagents under the
trivial names and abbreviations chemists actually write (`DIPEA`, `Cs2CO3`, `mCPBA`, `T3P`). It is
**first-party content, hand-authored in this repository**, not a third-party corpus. That is
deliberate for a first cut: it exercises every part of the mechanism — manifest, checksum, licence,
retrieval — while carrying no licensing question at all, and it is independently useful, since
`chemclaw/reagents.py` is a hand-maintained table and the hard ceiling on `resolve_compound`.

**No external dataset has been vendored yet.** Doing so is a build-pipeline step plus a licence
review, and both belong to whoever adds one — see below.

## Adding a dataset

1. Put `records.csv` and `dataset.json` in a directory under this one, or wherever the build
   installs it (`CHEMCLAW_VENDORED_DATASET_DIR`).
2. `dataset.json` must carry `name`, `version`, `licence`, `retrieved_from`, `description`,
   `sha256` and `text_column`. Every field is required: a corpus with no recorded licence is a
   legal question nobody can answer later, and one with no checksum cannot be shown to be what the
   review approved. `retrieved_from` is documentation of where a human obtained the file —
   nothing reads it as an address and nothing can fetch it.
3. Compute the checksum over the exact bytes that ship:
   `python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('records.csv').read_bytes()).hexdigest())"`
4. Enable it by adding `vendored` to `CHEMCLAW_DATA_SOURCES`. Off by default — a deployment that
   ships no dataset is unaffected.

If the shipped file ever stops matching its manifest, loading fails with both hashes named. The fix
is to rebuild the image, never to edit the manifest to match: the manifest records what was
reviewed, and editing it to agree with unreviewed bytes defeats the entire point of vendoring.
