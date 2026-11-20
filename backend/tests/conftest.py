import os

os.environ["DATABASE_URL"] = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev_test"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed


@pytest.fixture(scope="session", autouse=True)
def seeded():
    seed()
    yield


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(scope="session")
def auth(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}
