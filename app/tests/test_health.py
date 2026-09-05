from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>FaceChain Prototype</title>" in response.text
    assert 'id="consent-input"' not in response.text
