# `agents/` — MAF conversation layer

**Responsibility:** conversation orchestration and short reasoning steps, built on
the Microsoft Agent Framework. Agents advertise tools, load Skills on demand, and
kick off durable work — but they hold **no durability themselves** (that is
Temporal's job) and **no domain judgment** (that lives in `skills/`).

An agent tool that starts a long job returns immediately with a `job_id`; the
work runs as a Temporal workflow (see `workflows/`). See `docs/architektur.md` §1
and CLAUDE.md's four-layer rule.

Empty until Phase 1 (plan step 1.5). Becomes a Python package when the first
agent module lands.
