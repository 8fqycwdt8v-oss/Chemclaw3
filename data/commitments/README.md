# Commitment exports

Where the `commitments-json` data source reads a portfolio extract from
(`CHEMCLAW_COMMITMENT_EXPORT_DIR`, default `data/commitments`). One or more `*.json` files, each a
list of commitments or an object with a `commitments` key.

Empty in this repository, deliberately: a programme's committed work is a deployment's own data and
the source is off until `CHEMCLAW_DATA_SOURCES` names it.

The directory exists rather than being created on demand because its absence is *silent* — the
adapter finds no files, the mirror reports success with nothing mirrored, and `review_commitments`
reads the resulting NULL freshness as "nothing was ever mirrored". A wrong path and an empty
portfolio are indistinguishable to the person asking.
