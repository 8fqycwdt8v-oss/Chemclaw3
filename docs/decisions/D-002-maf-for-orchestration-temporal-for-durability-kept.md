# D-002 — MAF for orchestration, Temporal for durability (kept separate)

**Superseded-by:** [D-2026-08-10-langgraph-rebuild-of-the-conversation-layer](D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md) (in part)

MAF orchestrates the conversation and short reasoning steps; Temporal owns the lifecycle of
long scientific jobs (QM/DFT). Merging both durability models is explicitly avoided ("a
torturous path"). MAF ↔ Temporal integration is one thin DIY adapter (no official adapter
exists), not a framework. §1, §2, §15.
