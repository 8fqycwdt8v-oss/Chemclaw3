"""Layer 1, the conversation layer: the graph and its tools.

Tools are thin adapters over the layers below. None holds durable state: that lives in Temporal,
and the checkpointer holds only turn state.
"""
