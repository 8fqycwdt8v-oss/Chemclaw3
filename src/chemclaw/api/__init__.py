"""The front door: the ASGI service that runs the Chemclaw agent.

`create_app` (`chemclaw.api.app`) exposes the chat surface and turn API; `chemclaw.api.runner` owns
the per-turn lifecycle — build the agent, open connector sessions, run the turn and stream typed
events (`chemclaw.api.events`).
"""
