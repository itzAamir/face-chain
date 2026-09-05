from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from services.blockchain import BlockchainError, BlockchainService, normalize_digest
from services.evidence import EvidenceService
from services.face import (
    FaceModelsUnavailableError,
    FacePipelineError,
    FaceSelectionError,
    InvalidImageError,
    NoFaceDetectedError,
    FaceService,
)
from services.search import (
    SearchConfigurationError,
    SearchProviderError,
    SearchRateLimitError,
    SearchService,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
logger = logging.getLogger("facechain")
face_service = FaceService(settings)
search_service = SearchService(settings, face_service)
blockchain_service = BlockchainService(settings)
evidence_service = EvidenceService(
    settings, blockchain_service, image_fetcher=search_service.fetch_public_image
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await run_in_threadpool(face_service.warmup)
        logger.info("Face models loaded")
    except FaceModelsUnavailableError:
        logger.warning("Face models are unavailable")
    yield

app = FastAPI(
    title="FaceChain Verifier",
    version="0.2.0",
    description="Face selection and public reverse-image search pipeline.",
    lifespan=lifespan,
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
async def status() -> dict[str, object]:
    credential_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    google_configured = bool(credential_path and Path(credential_path).is_file())
    serpapi_configured = bool(settings.serpapi_api_key)
    return {
        "application": "ready",
        "face_pipeline": "ready" if face_service.models_ready else "models_missing",
        "web_search": "ready" if google_configured or serpapi_configured else "credentials_missing",
        "search_providers": {
            "google_web_detection": "configured" if google_configured else "not_configured",
            "serpapi_google_lens": "configured" if serpapi_configured else "not_configured",
        },
        "blockchain": await blockchain_service.status(),
        "attestation": "enabled" if settings.evidence_attest else "disabled",
    }


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def read_upload(image: UploadFile) -> bytes:
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise api_error(400, "unsupported_image_type", "Choose a JPG, PNG or WebP image.")
    content = await image.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise api_error(413, "image_too_large", "Choose an image smaller than 10 MB.")
    if not content:
        raise api_error(400, "empty_image", "The uploaded image is empty.")
    return content


@app.post("/api/faces/detect")
async def detect_faces(image: UploadFile = File(...)) -> dict[str, object]:
    content = await read_upload(image)
    try:
        decoded, faces = await run_in_threadpool(face_service.detect_bytes, content)
    except InvalidImageError as exc:
        raise api_error(400, "invalid_image", str(exc)) from exc
    except FaceModelsUnavailableError as exc:
        raise api_error(503, "face_models_unavailable", str(exc)) from exc
    if not faces:
        raise api_error(422, "no_face_detected", "No face was detected. Try a clearer photo.")
    height, width = decoded.shape[:2]
    return {
        "image": {"width": width, "height": height},
        "faces": [face.as_api_dict(width, height) for face in faces],
    }


@app.post("/api/search")
async def search(
    image: UploadFile = File(...), face_index: int = Form(...)
) -> dict[str, object]:
    content = await read_upload(image)
    try:
        decoded, faces = await run_in_threadpool(face_service.detect_bytes, content)
        selected = face_service.selected_face(decoded, faces, face_index)
        payload = await search_service.search(content, decoded, selected)
        # Anchoring runs after confirmation and never fails the search.
        return await evidence_service.attest_results(
            payload, query_image=content, face_index=selected.index
        )
    except InvalidImageError as exc:
        raise api_error(400, "invalid_image", str(exc)) from exc
    except NoFaceDetectedError as exc:
        raise api_error(422, "no_face_detected", str(exc)) from exc
    except FaceSelectionError as exc:
        raise api_error(422, "invalid_face_selection", str(exc)) from exc
    except FaceModelsUnavailableError as exc:
        raise api_error(503, "face_models_unavailable", str(exc)) from exc
    except SearchConfigurationError as exc:
        raise api_error(503, "search_not_configured", str(exc)) from exc
    except SearchRateLimitError as exc:
        raise api_error(429, "search_rate_limited", str(exc)) from exc
    except SearchProviderError as exc:
        raise api_error(502, "search_provider_failed", str(exc)) from exc
    except FacePipelineError as exc:
        raise api_error(422, "face_processing_failed", str(exc)) from exc


MAX_RECORD_BYTES = 1024 * 1024


@app.get("/api/evidence/{digest}")
def evidence(digest: str) -> dict[str, object]:
    """Return a stored record so it can be verified elsewhere."""
    try:
        normalized = normalize_digest(digest)
    except ValueError as exc:
        raise api_error(400, "invalid_digest", str(exc)) from exc
    bundle = evidence_service.load_record(normalized)
    if bundle is None:
        raise api_error(404, "evidence_not_found", "No record is stored for that digest.")
    return bundle


@app.post("/api/verify")
async def verify(
    record: UploadFile | None = File(default=None),
    digest: str | None = Form(default=None),
    refetch: bool = Form(default=False),
) -> JSONResponse:
    """Re-verify a record, or a bare digest, against the on-chain attestation."""
    if record is None and not digest:
        raise api_error(
            400, "nothing_to_verify", "Upload an evidence record or supply a digest."
        )
    try:
        if record is not None:
            content = await record.read(MAX_RECORD_BYTES + 1)
            if len(content) > MAX_RECORD_BYTES:
                raise api_error(413, "record_too_large", "That record file is too large.")
            try:
                bundle = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise api_error(400, "invalid_record", "That file is not valid JSON.") from exc
            if not isinstance(bundle, dict):
                raise api_error(400, "invalid_record", "An evidence record must be a JSON object.")
            result = await evidence_service.verify_bundle(bundle, refetch=refetch)
        else:
            try:
                normalized = normalize_digest(digest or "")
            except ValueError as exc:
                raise api_error(400, "invalid_digest", str(exc)) from exc
            result = await evidence_service.verify_digest(normalized)
    except BlockchainError as exc:  # pragma: no cover - verification absorbs these
        raise api_error(503, "blockchain_unavailable", str(exc)) from exc

    # A failed verification is a valid answer, not a transport error, but it must
    # not read as success to a client that only checks the status code.
    ok = result.get("status") in {"verified", "not_attested"}
    return JSONResponse(result, status_code=200 if ok else 422)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
