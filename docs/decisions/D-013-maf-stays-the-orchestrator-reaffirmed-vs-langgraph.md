# D-013 — MAF stays the orchestrator (reaffirmed vs. LangGraph)

Reconsidered MAF vs. LangGraph explicitly. LangGraph's main edge (durable/checkpointed execution)
is largely moot here because durability lives in Temporal (D-002); MAF's native Agent-Skills
(SKILL.md progressive disclosure) and Entra/Azure fit are load-bearing for our design. The agent
layer is kept thin and framework-swappable, bounding MAF's maturity risk. Decision: keep MAF.
