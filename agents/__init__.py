"""MAF conversation layer: the agent and its tools.

The agent orchestrates the conversation and advertises tools; the tools are the
one thin adapter between MAF and Temporal (D-002) — they start/query durable
workflows and return immediately, holding no durable state themselves.
"""
