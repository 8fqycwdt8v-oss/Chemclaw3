"""Chemclaw shared kernel.

This package holds the *only* cross-cutting pieces that every layer may import —
today just the typed configuration (`chemclaw.core.config`). The four architectural
layers live in their own sibling packages (`agent/`, `durable/`, `connectors/`,
`kg/`) and pull environment values exclusively from here, so there is a single
source of truth for configuration (see CLAUDE.md, plan step 0.3).
"""
