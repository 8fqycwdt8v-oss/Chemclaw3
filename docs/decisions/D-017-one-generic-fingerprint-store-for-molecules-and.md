# D-017 — One generic fingerprint store for molecules and reactions

Reactions (DRFP) are the second fingerprint domain after molecules (ECFP4), so the
Rule of Three fired and the Tanimoto ranking, the record/Match types, the store Protocol,
and both backends (in-memory + Postgres) live once in `mcp_servers/fpstore.py`. A record is
a neutral `(id, label, bits)`; each domain supplies only its fingerprint function, its table
name, and its bit width (constructor params, both trusted constants). This mirrors the
calculation store (D-011): one ranking contract, swappable backend, no per-domain copy. The
molecule table column was renamed `smiles → label` to match (greenfield, CI recreates the DB).
