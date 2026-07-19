"""Compute layer: the calculation result store and (later) the calculators.

Everything expensive — xTB/GFN2, ML predictors, BO objective evaluations, and one
day HPC/DFT — runs through the store here so an identical calculation is never
performed twice (D-011). The store is the single persistence path shared by every
calculator; calculator implementations and their registry land in Phase 1c.
"""
