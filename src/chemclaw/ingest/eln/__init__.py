"""ELN ingestion: raw electronic-lab-notebook entries into validated, canonical `OrdReaction` records.

The ORD-based target schema (`chemclaw.ingest.eln.ord`) is ELN-agnostic; every ELN-specific quirk is
confined to a concrete adapter behind the `ElnAdapter` contract (`chemclaw.ingest.eln.adapter`), so
nothing above the adapter knows any ELN's shape.
"""
