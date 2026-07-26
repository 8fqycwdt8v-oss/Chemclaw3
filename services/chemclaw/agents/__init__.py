"""MAF conversation layer: the agent and its tools.

The agent orchestrates the conversation and advertises tools; the tools are thin
adapters over the layers below. Most call into fast in-process capability (calc, BO,
knowledge-graph read, evidence retrieval) and return directly; the QM and approval
tools are the thin adapter between MAF and Temporal (D-002), starting/querying durable
workflows and returning immediately. No tool holds durable state — that lives in Temporal.
"""
