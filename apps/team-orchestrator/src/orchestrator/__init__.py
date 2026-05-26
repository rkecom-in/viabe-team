"""Viabe Team orchestrator-agent runtime.

VT-3.1 ships the substrate only: a LangGraph state graph wrapped in DBOS
durable workflows. No LLM calls, no reasoning, no tools (Pillar 1) — the
orchestrator-agent that reasons over coordination lands in VT-3.9.

VT-179 typed-envelope registry drift check is invoked from the orchestrator
process startup paths — ``dbos_config.launch_dbos()`` and the FastAPI
``main.py`` lifespan — NOT at package import time. Package import time
would transitively pull psycopg via ``observability/__init__.py`` eager
re-exports, breaking minimal-deps CI test runs (CL-419 / VT-179 fix-1).
"""
