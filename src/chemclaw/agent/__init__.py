"""Layer 1, the conversation layer: the graph and its tools.

The compiled graph orchestrates the conversation and advertises tools; the tools are thin
adapters over the layers below. Most call into fast in-process capability (calc, BO,
knowledge-graph read, evidence retrieval) and return directly; the QM and approval
tools are the thin adapter between this layer and Temporal (D-002), starting/querying durable
workflows and returning immediately. No tool holds durable state — that lives in Temporal, and
the checkpointer under the graph holds this turn's state and nothing more.
"""
