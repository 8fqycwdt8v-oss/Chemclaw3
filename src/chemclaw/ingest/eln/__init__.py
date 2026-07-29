"""ELN ingestion (plan Phase 4).

The integration layer that turns raw electronic-lab-notebook entries into validated,
canonical `OrdReaction` records. The ORD-based target schema (`chemclaw.ingest.eln.ord`) is stable
and
ELN-agnostic; every ELN-specific quirk is confined to a concrete adapter
(`chemclaw.ingest.eln.json_adapter`)
behind the `ElnAdapter` contract (`chemclaw.ingest.eln.adapter`), so nothing above the adapter
knows any
ELN's shape (G6).
"""
