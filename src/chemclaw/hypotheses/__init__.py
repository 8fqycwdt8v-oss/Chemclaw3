"""Generating competing hypotheses, ranking them by judged comparison, and saying how firmly.

Pure computation: the rating fit, the Swiss pairing, the mechanical screen and the rendering. It
imports no Temporal, no LangGraph and no model client — `durable/hypothesis_tournament.py` is the
orchestration that calls a model and this is the arithmetic it calls between those calls, the same
split `science/bo` has against the `bo` bundle.
"""
