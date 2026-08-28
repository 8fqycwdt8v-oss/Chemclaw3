# Live corpus-fidelity pass

Ground truth: the published factor tables · Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`
· 20.0s

Backfill: eln-backfill-epoch: still draining after 20s — the workflow keeps running on the broker, so re-running this lane later reads the finished corpus

No checks run (`--backfill-only`). `make live-data` reads what arrived.
