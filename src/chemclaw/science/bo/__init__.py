"""Bayesian optimization layer.

BoFire is kept behind neutral problem/observation types (`chemclaw.science.bo.problem`) so agents,
skills and workflows never import it; `chemclaw.science.bo.engine` is the only module that does. The
ask/tell loop is the durable Temporal `BoCampaignWorkflow` (`connectors/bo/workflows.py`).
"""
