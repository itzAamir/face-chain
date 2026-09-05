from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _int_env(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float_env(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool_env(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    max_upload_bytes: int = _int_env("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    max_image_pixels: int = _int_env("MAX_IMAGE_PIXELS", 24_000_000)
    face_detection_threshold: float = _float_env("FACE_DETECTION_THRESHOLD", 0.9)
    candidate_face_detection_threshold: float = _float_env(
        "CANDIDATE_FACE_DETECTION_THRESHOLD", 0.65
    )
    face_match_threshold: float = _float_env("FACE_MATCH_THRESHOLD", 0.363)
    candidate_limit: int = _int_env("SEARCH_CANDIDATE_LIMIT", 80)
    result_limit: int = _int_env("SEARCH_RESULT_LIMIT", 5)
    per_page_image_limit: int = _int_env("SEARCH_IMAGES_PER_PAGE", 4)
    max_page_bytes: int = _int_env("MAX_CANDIDATE_PAGE_BYTES", 2 * 1024 * 1024)
    provider_timeout_seconds: float = _float_env("SEARCH_PROVIDER_TIMEOUT_SECONDS", 25.0)
    download_timeout_seconds: float = _float_env("CANDIDATE_DOWNLOAD_TIMEOUT_SECONDS", 10.0)
    max_candidate_bytes: int = _int_env("MAX_CANDIDATE_BYTES", 10 * 1024 * 1024)
    download_concurrency: int = _int_env("CANDIDATE_DOWNLOAD_CONCURRENCY", 4)
    max_redirects: int = _int_env("CANDIDATE_MAX_REDIRECTS", 3)
    serpapi_timeout_seconds: float = _float_env("SERPAPI_TIMEOUT_SECONDS", 45.0)
    blockchain_rpc_url: str = os.getenv("BLOCKCHAIN_RPC_URL", "http://blockchain:8545")
    contract_deployment_file: Path = Path(
        os.getenv("CONTRACT_DEPLOYMENT_FILE", "/deployment/contract.json")
    )
    attester_address: str | None = os.getenv("ATTESTER_ADDRESS") or None
    blockchain_timeout_seconds: float = _float_env("BLOCKCHAIN_TIMEOUT_SECONDS", 10.0)
    blockchain_receipt_attempts: int = _int_env("BLOCKCHAIN_RECEIPT_ATTEMPTS", 20)
    blockchain_receipt_interval_seconds: float = _float_env(
        "BLOCKCHAIN_RECEIPT_INTERVAL_SECONDS", 0.25
    )
    evidence_dir: Path = Path(os.getenv("EVIDENCE_DIR", "/data/evidence"))
    evidence_attest: bool = _bool_env("EVIDENCE_ATTEST", False)
    # Retains the exact bytes that were hashed, so a deleted post can still be
    # re-verified. Off by default: it is the only part of the pipeline that
    # persists image data.
    evidence_store_images: bool = _bool_env("EVIDENCE_STORE_IMAGES", False)
    serpapi_api_key: str | None = field(
        default=os.getenv("SERPAPI_API_KEY") or None,
        repr=False,
    )
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
