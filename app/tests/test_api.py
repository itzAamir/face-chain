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
            "providers": ["google_web_detection"],
            "query_face": {"index": 0},
            "summary": {
                "candidates_examined": 0,
                "candidate_images_examined": 0,
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


def test_status_reports_the_chain(monkeypatch) -> None:
    async def fake_status() -> dict[str, object]:
        return {"state": "ready", "chain_id": 31337, "contract_address": "0xabc"}

    monkeypatch.setattr(main.blockchain_service, "status", fake_status)
    body = client.get("/api/status").json()
    assert body["blockchain"]["state"] == "ready"
    assert body["blockchain"]["chain_id"] == 31337
    assert body["attestation"] in {"enabled", "disabled"}


def test_verify_requires_something_to_check() -> None:
    response = client.post("/api/verify")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "nothing_to_verify"


def test_verify_rejects_a_malformed_digest() -> None:
    response = client.post("/api/verify", data={"digest": "not-a-digest"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_digest"


def test_verify_rejects_a_non_json_upload() -> None:
    response = client.post(
        "/api/verify",
        files={"record": ("record.json", io.BytesIO(b"not json"), "application/json")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_record"


def test_verify_returns_200_for_a_verified_record(monkeypatch) -> None:
    async def fake_verify(bundle, refetch=False) -> dict[str, object]:
        assert refetch is True
        return {"status": "verified", "record_digest": "0x" + "ab" * 32}

    monkeypatch.setattr(main.evidence_service, "verify_bundle", fake_verify)
    response = client.post(
        "/api/verify",
        files={"record": ("record.json", io.BytesIO(b'{"post": {}}'), "application/json")},
        data={"refetch": "true"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "verified"


def test_verify_returns_422_for_a_tampered_record(monkeypatch) -> None:
    """A failed verification must not read as success to a status-code check."""

    async def fake_verify(bundle, refetch=False) -> dict[str, object]:
        return {"status": "digest_mismatch", "reason": "modified"}

    monkeypatch.setattr(main.evidence_service, "verify_bundle", fake_verify)
    response = client.post(
        "/api/verify",
        files={"record": ("record.json", io.BytesIO(b'{"post": {}}'), "application/json")},
    )
    assert response.status_code == 422
    assert response.json()["status"] == "digest_mismatch"


def test_verify_by_digest(monkeypatch) -> None:
    async def fake_verify_digest(digest: str) -> dict[str, object]:
        return {"status": "not_attested", "record_digest": digest}

    monkeypatch.setattr(main.evidence_service, "verify_digest", fake_verify_digest)
    response = client.post("/api/verify", data={"digest": "ab" * 32})
    assert response.status_code == 200
    assert response.json()["record_digest"] == "0x" + "ab" * 32


def test_evidence_returns_a_stored_record(monkeypatch) -> None:
    bundle = {"record": {"post": {}}, "proof": {"state": "attested"}}
    monkeypatch.setattr(main.evidence_service, "load_record", lambda digest: bundle)
    response = client.get("/api/evidence/0x" + "ab" * 32)
    assert response.status_code == 200
    assert response.json() == bundle


def test_evidence_reports_a_missing_record(monkeypatch) -> None:
    monkeypatch.setattr(main.evidence_service, "load_record", lambda digest: None)
    response = client.get("/api/evidence/0x" + "ab" * 32)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "evidence_not_found"


def test_evidence_rejects_a_malformed_digest() -> None:
    response = client.get("/api/evidence/nope")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_digest"


def test_search_attaches_proofs(monkeypatch) -> None:
    """The search route must run the anchoring stage, not just discovery."""
    monkeypatch.setattr(main.face_service, "detect_bytes", fake_detect)

    async def fake_search(*_) -> dict[str, object]:
        return {"status": "matched", "results": [{"page_url": "https://x/1"}]}

    async def fake_attest(payload, *, query_image, face_index) -> dict[str, object]:
        assert query_image == b"fake-image" and face_index == 0
        payload["results"][0]["proof"] = {"state": "attested"}
        return payload

    monkeypatch.setattr(main.search_service, "search", fake_search)
    monkeypatch.setattr(main.evidence_service, "attest_results", fake_attest)
    response = client.post("/api/search", files=IMAGE_UPLOAD, data={"face_index": "0"})
    assert response.status_code == 200
    assert response.json()["results"][0]["proof"]["state"] == "attested"
