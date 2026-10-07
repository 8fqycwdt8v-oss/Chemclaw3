"""Agent memory layers: episodic and semantic, built from existing pieces.

The episodic layer (`chemclaw.memory.campaign`) chains experiments where one reaction's product is
another's reactant and narrates the chain as a `campaign` note. The semantic layer
(`chemclaw.memory.playbook`) distils patterns recurring across >=2 projects into a `playbook` note.
Both write through `kg/record.py`; there is no separate store.
"""
