"""Bayesian optimization layer (plan Phase 1d).

BoFire is the BO engine, kept behind our own neutral problem/observation types
(`bo.problem`) so agents, skills, and workflows never import BoFire directly
(D-012, gate G6). `bo.engine` is the only module that touches BoFire; `bo.campaign`
runs the ask/tell loop that will later become a durable Temporal workflow.
"""
