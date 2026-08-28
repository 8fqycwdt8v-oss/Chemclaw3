# 02 — Acquisition: pulling from an upstream database over a URL

The first two stages of a feeder — **acquire** and **normalise** — are the same on both platforms, so
they are written once here. `03-databricks.md` and `04-openshift.md` pick this up at stage 3.

Nothing in this file is enforced by ChemClaw3. It is the set of properties that make a *daily* pull
survivable, learned from the failure modes the contract in `01-contract.md` cannot see.

---

## 1. The five properties a daily pull needs

A one-off load needs none of these. A recurring one needs all five, and each one is cheap to build in
and expensive to retrofit.

| Property | What it means | What its absence looks like on day 40 |
| --- | --- | --- |
| **Idempotent** | running the same day twice changes nothing | duplicate reactions under two ids; `corpus_molecules` still correct (it dedupes by structure), the precedent count silently doubled |
| **Resumable** | a run that dies at 60% restarts at 60% | a 4-hour job that can only ever be re-run from zero, so it is never re-run |
| **Watermarked upstream** | the cursor is a value *the upstream defines* | rows lost across a run that partially succeeded, because "since I last ran" was measured on your clock |
| **Verified** | the bytes are the bytes that were published | a truncated download landing as a short release nobody notices |
| **Provenanced** | the row remembers where it came from and under what licence | a legal question in a year that nobody can answer |

## 2. Landing is two tables, not one

```
  upstream  ──►  raw_<source>       immutable, append-only, exactly as received
                      │             (+ release id, retrieved_at, source URL, sha256)
                      ▼
                 v_reaction          the contract relation (01-contract.md §1)
```

**Keep the raw layer.** It is what makes stage 2 re-runnable when — not if — you discover the
normalisation was wrong: a mis-parsed yield column, an agent slot dropped, a date read as US format.
Without it, fixing a normalisation bug means re-downloading a release the vendor may have replaced.

The raw layer is also where a **quarantine** belongs. A row whose SMILES will not parse, or that has
no citation, is going to be skipped by ChemClaw3's drain and counted in `CorpusReport.skipped` — a
number on a dashboard with no way back to the row. Write it to `raw_<source>_rejected` with the reason
instead, and count it. A rejection rate that moves is the earliest signal that upstream changed
something.

## 3. Identifiers: the one decision that cannot be revised

`key:` is the join between the relation, the vector index, `reaction_labels`, `reaction_species` and
every citation an answer carries. It must be **stable for the same reaction across releases**.

- **If upstream has a stable id, use it.** Do not prefix it, do not normalise its case, do not "clean"
  it. Every transformation is a chance to change it in release *n+1*.
- **If it does not**, derive one deterministically and write the recipe down where it cannot be lost:

  ```
  reaction_id = <source>:<sha256(citation || ':' || reaction_index_within_document)[:16]>
  ```

  Base it on properties of the *document*, never on row order in the file and never on the reaction
  SMILES itself — a corrected SMILES must keep its id, or the correction lands as a new reaction and
  the wrong one stays.

- **Never reuse an id for a different reaction.** If upstream does, prefix with the release.

Changing the recipe later is not a migration; it is a new corpus. Everything keyed on the old ids —
the index, the labels, the species rows — points at reactions that no longer exist, and nothing
notices, because ChemClaw3 drops a key the relation no longer holds rather than raising on it.

## 4. Watermarks and windows

**The cursor is a value the upstream defines**, recorded only after the run that consumed it
succeeded. Three shapes, in decreasing order of how much you can trust them:

| Upstream offers | Cursor | Caveat |
| --- | --- | --- |
| a release/version id | that id | the honest case: a release is a versioned load |
| a monotone sequence or change-id | the highest consumed | verify it really is monotone under concurrent writers |
| a modification timestamp | `max(modified_at)` **minus an overlap** | clock skew and open transactions; the overlap is the whole safety margin |

For the timestamp case, re-read a window that overlaps the last one — an hour, a day — and rely on
the upsert to make the overlap free. A watermark with no overlap loses every row committed with a
timestamp earlier than one already consumed.

Then, separately, set the `load_date` your own `where:` window reads (`01-contract.md` §5). These are
two different clocks and conflating them is a classic: the upstream watermark says *what you have
consumed*, `load_date` says *what ChemClaw3 has not yet re-read*.

## 5. Fetching politely, and verifiably

- **Retry with exponential backoff and a cap**, on connection errors and on 5xx/429 only. Honour
  `Retry-After`. A 4xx that is not 429 is a bug or an expired licence — fail the run loudly rather
  than retrying into a rate limit.
- **Resume with `Range:`** for a multi-gigabyte artifact, and re-verify the whole file afterwards.
- **Verify the digest.** If the publisher provides one, check it. If not, compute and record your own,
  so a re-download can be compared and a replaced release is visible.
- **Set a real `User-Agent`** naming the site and a contact. It is what gets you a mail instead of a
  block when a schedule misbehaves.
- **Bound the run.** A daily job with no timeout eventually overlaps itself; both platforms in this
  guide refuse the overlap, but the hung run still holds its resources.

## 6. Provenance, mirroring the fleet's rule for vendored data

`Chemclaw3-mcp` requires every vendored corpus to ship a `dataset.json` carrying `name`, `version`,
`licence`, `retrieved_from`, `description` and `sha256` — all six required, because "a corpus with no
recorded licence is a legal question nobody can answer a year later, one with no checksum cannot be
shown to be what the review approved, and `retrieved_from` is the only record of where a human
obtained the file."

The same reasoning applies to a feeder, with the difference that the record belongs in a *table*,
one row per release:

| Column | Why |
| --- | --- |
| `release_id` | what the rows in this batch belong to |
| `source_name`, `source_url` | where it came from |
| `retrieved_at` | when |
| `sha256`, `bytes` | what was actually fetched |
| `licence` | the answer to the question that gets asked a year later |
| `row_count`, `rejected_count` | what the run produced, so a bad release is visible as a number |

And the same caveat holds: **nothing reads `source_url` as an address.** It is a record of where a
human obtained the file, not a runtime dependency.

## 7. Schema drift

Upstream will add a column, rename one, or change a unit. Two defences, both cheap:

- **Assert the columns you read exist, at the start of stage 2**, and fail the run naming the missing
  one. A silent `NULL` propagates all the way to a row ChemClaw3 skips.
- **Carry the rest anyway.** The raw layer keeps everything; that is what makes a column added next
  quarter visible without a code change. (ChemClaw3's ELN binding does the same thing with its
  `attributes:` block — the corpus shape has no equivalent, so the raw layer is where it lives.)

A renamed column is the one case worth alerting on immediately: the run keeps succeeding, the field
goes empty, and the contract's "a path that does not resolve is silence, not an error" turns it into
a corpus that quietly loses its yields.

## 8. Three upstream shapes

### 8.1 A bulk release published as files over HTTPS

The common case for an open corpus (the Open Reaction Database's releases, a SureChEMBL-style patent
bulk export, a vendor's quarterly drop).

1. Fetch the release manifest; compare its version to the last consumed. Nothing new → exit 0 with a
   "no new release" record, not a failure.
2. Download each artifact with resume; verify digests.
3. Land raw, one row per record, with the release id.
4. Normalise into `v_reaction`; stamp `load_date`.
5. Record the release row (§6) **last** — it is the commit marker, so a run that dies mid-way is
   re-run rather than skipped.

Cadence: a release changes when the publisher ships one. A daily *check* is fine; a daily *download*
of an unchanged release is not.

### 8.2 A delta API — new reactions since a cursor

The shape the original ask describes: "new reaction SMILES plus reaction identifier".

1. Read the stored cursor; request the next page.
2. Land raw; normalise; stamp `load_date`.
3. Advance the cursor **only after the page is committed**, and only to a value the upstream gave you.
4. Loop until the API says there is no more, or until a page budget is spent — then exit successfully
   and let the next run continue. A feeder that must finish in one run is a feeder that fails during
   a backlog.

Cadence: daily, or hourly if the upstream is genuinely live. Then reconsider ChemClaw3's
`CHEMCLAW_CORPUS_SYNC_SCHEDULE_MINUTES`, which is 1440 for the corpus drain because a release "changes
when a vendor ships one, not continuously" — an assumption a live feed breaks.

### 8.3 A database you can query directly

An internal reaction store, a partner's read replica, an existing lake table. The pull is a query, and
the two things that matter are the same: a cursor the *source* defines, and a bounded page.

If it is a table in the same lakehouse ChemClaw3 already reads, ask whether you need a feeder at all —
point the binding at it and let the drain do the walking (`01-contract.md`). You need a feeder only if
something must be *computed* (the embedding), *normalised* (the SMILES assembled into
`reactants>agents>products`), or *fetched* (anything over a URL).

## 9. Normalisation checklist

Stage 2 turns a raw row into a contract row. What has to be true when it is done:

- [ ] `reaction_id` present, stable, unique (§3)
- [ ] reaction SMILES assembled as `reactants>agents>products`, **agents kept** (`01-contract.md` §1.1)
- [ ] the SMILES parses — reject to quarantine, do not write a row ChemClaw3 will skip
- [ ] citation present, and resolvable by a human
- [ ] units converted to the contract's: °C, hours, percent (do it here — the binding's transform
      vocabulary is closed and deliberately cannot express arbitrary arithmetic)
- [ ] dates as ISO
- [ ] `load_date` stamped on every row written or corrected (`01-contract.md` §5)
- [ ] column casing matches what the binding will read (`01-contract.md` §1.5)
- [ ] rejects counted, with a reason, and the count trended
