import io

import numpy as np
from fastapi.testclient import TestClient

import main
from services.face import DetectedFace
from services.search import SearchConfigurationError, SearchProviderError, SearchRateLimitError


client = TestClient(main.app)
IMAGE_UPLOAD = {"image": ("face.jpg", io.BytesIO(b"fake-image"), "image/jpeg")}


def fake_detect(_: bytes) -> tuple[np.ndarray, list[DetectedFace]]:
    raw = np.array([10, 10, 20, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.99], dtype=np.float32)
    return np.zeros((100, 200, 3), dtype=np.uint8), [DetectedFace(0, raw)]


def test_detect_faces_returns_normalized_box(monkeypatch) -> None:
    monkeypatch.setattr(main.face_service, "detect_bytes", fake_detect)
    response = client.post("/api/faces/detect", files=IMAGE_UPLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["image"] == {"width": 200, "height": 100}
    assert body["faces"][0]["box"]["x"] == 0.05


def test_detect_faces_rejects_unsupported_type() -> None:
    response = client.post(
        "/api/faces/detect",
        files={"image": ("face.gif", io.BytesIO(b"gif"), "image/gif")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_image_type"


def test_search_returns_service_result(monkeypatch) -> None:
    monkeypatch.setattr(main.face_service, "detect_bytes", fake_detect)

    async def fake_search(*_) -> dict[str, object]:
        return {
            "status": "no_match",
            "provider": "google_web_detection",
            "query_face": {"index": 0},
            "summary": {
                "candidates_examined": 0,
                "confirmed_results": 0,
                "confirmed_social_posts": 0,
            },
            "results": [],
        }

    monkeypatch.setattr(main.search_service, "search", fake_search)
    response = client.post("/api/search", files=IMAGE_UPLOAD, data={"face_index": "0"})
    assert response.status_code == 200
    assert response.json()["status"] == "no_match"


def test_search_rejects_invalid_face_index(monkeypatch) -> None:
    monkeypatch.setattr(main.face_service, "detect_bytes", fake_detect)
    response = client.post("/api/search", files=IMAGE_UPLOAD, data={"face_index": "3"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_face_selection"


def test_detect_reports_no_face(monkeypatch) -> None:
    monkeypatch.setattr(
        main.face_service,
        "detect_bytes",
        lambda _: (np.zeros((100, 100, 3), dtype=np.uint8), []),
    )
    response = client.post("/api/faces/detect", files=IMAGE_UPLOAD)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_face_detected"


def test_search_maps_provider_failures(monkeypatch) -> None:
    monkeypatch.setattr(main.face_service, "detect_bytes", fake_detect)

    cases = (
        (SearchConfigurationError("not configured"), 503, "search_not_configured"),
        (SearchRateLimitError("rate limited"), 429, "search_rate_limited"),
        (SearchProviderError("provider failed"), 502, "search_provider_failed"),
    )
    for error, status_code, code in cases:
        async def fail(*_, current=error) -> dict[str, object]:
            raise current

        monkeypatch.setattr(main.search_service, "search", fail)
        response = client.post("/api/search", files=IMAGE_UPLOAD, data={"face_index": "0"})
        assert response.status_code == status_code
        assert response.json()["detail"]["code"] == code
