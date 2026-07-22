"""The API skeleton (FastAPI + Mangum) is importable and healthy."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, handler


def test_health():
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["region"] == "ap-south-2"


def test_mangum_handler_present():
    assert callable(handler)
