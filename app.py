"""Vercel FastAPI entry point. Writable runtime data stays in /tmp."""
import os

if os.getenv("VERCEL"):
    os.environ.setdefault("SO_CACHE_DIR", "/tmp/backstop/cache")
    os.environ.setdefault("SO_LOG_DIR", "/tmp/backstop/logs")
    os.environ.setdefault("SERVER_LOG_DIR", "/tmp/backstop/logs")
    os.environ.setdefault("BACKSTOP_USAGE_DB", "/tmp/backstop/usage.db")

from server.app import app  # noqa: E402,F401
