"""Explicit bearer authentication and isolated rate limits for offline race tests."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api_v1
from server.usage import RateLimiter

KEY = "test-only-race-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def authenticated_client(app: FastAPI) -> TestClient:
    limiter = RateLimiter(limit=60)
    app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
    return TestClient(app, headers=AUTH)
