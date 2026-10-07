"""The connector seam: one standardized way to add a capability to Chemclaw.

A connector is a folder — `connectors/<name>/connector.yaml` plus what it needs — declaring its MCP
tools, its durable jobs, the skills that teach them and the agent profiles they enable. It is
discovered by folder, validated by a pydantic manifest, enabled by one config token and checked by
`make connector-validate`; adding a capability never edits orchestration code.

The durable half lives in `durable/connector_job.py`: core's `ConnectorJobWorkflow` keeps
idempotency, actor attribution, the knowledge-graph write and session push-back, while the
connector owns the workflow it wraps.
"""
