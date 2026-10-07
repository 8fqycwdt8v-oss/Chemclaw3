"""Chemclaw shared kernel.

Admission rule: a module belongs here if every layer may need to import it and it imports no
sibling layer in return (configuration, clients, ids, logging, errors, embeddings, chemistry
helpers, metrics, the bounded LRU, and the ambient-turn primitives). `core` has no module-scope
import of another first-party package and one declared lazy exception (`logging`'s redaction
filter resolving connector token env-var names); `tests/test_layering.py` enforces both. See
`src/chemclaw/core/README.md`.
"""
