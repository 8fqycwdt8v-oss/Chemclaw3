"""Bayesian optimization layer (plan Phase 1d).

BoFire is the BO engine, kept behind our own neutral problem/observation types
(`chemclaw.science.bo.problem`) so agents, skills, and workflows never import BoFire directly
(D-012, gate G6). `chemclaw.science.bo.engine` is the only module that touches BoFire, and the
ask/tell loop over it is the Temporal `BoCampaignWorkflow` (`connectors/bo/workflows.py`) — durable
and resumable, which is the only form a campaign ships in. The in-process loop that predated it
lived here with no caller for months and now lives in `tests/bo_harness.py`, where its one real
audience is.
"""
