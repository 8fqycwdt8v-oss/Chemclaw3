"""Generating competing hypotheses, ranking them by judged comparison, and saying how firmly.

Pure computation: the rating fit, Swiss pairing, the mechanical screen and rendering. No Temporal,
LangGraph or model client; `durable/hypothesis_tournament.py` orchestrates the model calls around
it.
"""
