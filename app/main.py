from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
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
def status() -> dict[str, object]:
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
        "blockchain": "next_build",
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
        return await search_service.search(content, decoded, selected)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
