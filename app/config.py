from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _int_env(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    max_upload_bytes: int = _int_env("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    max_image_pixels: int = _int_env("MAX_IMAGE_PIXELS", 24_000_000)
    face_detection_threshold: float = _float_env("FACE_DETECTION_THRESHOLD", 0.9)
    face_match_threshold: float = _float_env("FACE_MATCH_THRESHOLD", 0.363)
    candidate_limit: int = _int_env("SEARCH_CANDIDATE_LIMIT", 20)
    result_limit: int = _int_env("SEARCH_RESULT_LIMIT", 5)
    provider_timeout_seconds: float = _float_env("SEARCH_PROVIDER_TIMEOUT_SECONDS", 25.0)
    download_timeout_seconds: float = _float_env("CANDIDATE_DOWNLOAD_TIMEOUT_SECONDS", 10.0)
    max_candidate_bytes: int = _int_env("MAX_CANDIDATE_BYTES", 10 * 1024 * 1024)
    download_concurrency: int = _int_env("CANDIDATE_DOWNLOAD_CONCURRENCY", 4)
    max_redirects: int = _int_env("CANDIDATE_MAX_REDIRECTS", 3)
    yunet_model: Path = Path(
        os.getenv(
            "YUNET_MODEL_PATH",
            str(BASE_DIR / "models" / "face_detection_yunet_2023mar.onnx"),
        )
    )
    sface_model: Path = Path(
        os.getenv(
            "SFACE_MODEL_PATH",
            str(BASE_DIR / "models" / "face_recognition_sface_2021dec.onnx"),
        )
    )


settings = Settings()
