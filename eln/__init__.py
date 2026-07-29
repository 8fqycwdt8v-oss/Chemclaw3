"""ELN ingestion (plan Phase 4).

The integration layer that turns raw electronic-lab-notebook entries into validated,
canonical `OrdReaction` records. The ORD-based target schema (`eln.ord`) is stable and
ELN-agnostic; every ELN-specific quirk is confined to a concrete adapter (`eln.json_adapter`)
behind the `ElnAdapter` contract (`eln.adapter`), so nothing above the adapter knows any
ELN's shape (G6).
"""
