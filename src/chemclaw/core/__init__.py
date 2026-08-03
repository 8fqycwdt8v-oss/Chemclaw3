"""Chemclaw shared kernel.

The admission rule, not a file list — a list drifts, this does not: a module belongs here if
every layer may need to import it and it imports no sibling layer in return. That covers typed
configuration, the database/HTTP/Temporal clients, id generation, structured logging, the error
taxonomy, embeddings and chemistry helpers, the process-wide metrics registry, the shared
bounded-LRU, and the ambient-turn primitives (identity, session id, turn signals, the
capability-tool registry) every layer stamps or reads on a turn.

`core` has zero module-scope import of another first-party package, and exactly one declared
*lazy* exception (`logging`'s redaction filter resolving a connector's bearer-token env-var name
inside a function, not at import time) — `tests/test_layering.py` enforces both halves, so a
module that broke the rule would fail there rather than merely read wrong here. See
`src/chemclaw/core/README.md` for what lives here today and why each piece qualifies.
"""
