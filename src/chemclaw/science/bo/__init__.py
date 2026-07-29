"""Bayesian optimization layer (plan Phase 1d).

BoFire is the BO engine, kept behind our own neutral problem/observation types
(`chemclaw.science.bo.problem`) so agents, skills, and workflows never import BoFire directly
(D-012, gate G6). `chemclaw.science.bo.engine` is the only module that touches BoFire;
`chemclaw.science.bo.campaign`
runs the ask/tell loop that will later become a durable Temporal workflow.
"""
