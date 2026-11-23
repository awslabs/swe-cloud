"""Client-side processes: the worker that submits runs, the supervisor, and the flow engine.

These are the deployed clients. They talk to the orchestrator over its HTTP API and record flow
attempts in Postgres (attempt_log); they are not changed with server fixes.
"""
