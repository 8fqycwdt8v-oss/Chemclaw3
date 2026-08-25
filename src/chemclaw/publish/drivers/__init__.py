"""The shipped result-sink drivers: one that speaks SQL, one that speaks HTTP.

Two rather than one, deliberately. The `ResultSink` Protocol with a single implementation would be
an abstraction with one caller, which this codebase inlines on sight (Rule of Three). Two real
implementations is what makes the seam earn itself — and they cover the two shapes a site's results
store actually takes: a database a DBA runs our DDL on, and a service that accepts a document.
"""
