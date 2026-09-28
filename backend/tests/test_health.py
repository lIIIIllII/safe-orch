from fastapi.testclient import TestClient

from app.main import app
from app.store.db import SCHEMA_VERSION


def test_health():
    with TestClient(app) as client:
        res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["db"] == "connected"
    assert body["sqlite_version"]
    assert body["schema_version"] == SCHEMA_VERSION
