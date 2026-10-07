"""Bayesian optimization layer.

BoFire stays behind neutral problem/observation types (`problem`); `engine` is the only module that
imports it. The durable ask/tell loop is `connectors/bo/workflows.py`'s `BoCampaignWorkflow`.
"""
