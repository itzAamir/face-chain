"""In-memory YuNet face detection and SFace recognition helpers."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass

import cv2
import numpy as np

from config import Settings


class FacePipelineError(ValueError):
    """Base class for safe, user-facing face pipeline failures."""


class InvalidImageError(FacePipelineError):
    pass


class NoFaceDetectedError(FacePipelineError):
    pass


class FaceSelectionError(FacePipelineError):
    pass


class FaceModelsUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectedFace:
    index: int
    raw: np.ndarray

    @property
    def x(self) -> float:
        return float(self.raw[0])

    @property
    def y(self) -> float:
        return float(self.raw[1])

    @property
    def width(self) -> float:
        return float(self.raw[2])

    @property
    def height(self) -> float:
        return float(self.raw[3])

    @property
    def confidence(self) -> float:
        return float(self.raw[-1])

    def as_api_dict(self, image_width: int, image_height: int) -> dict[str, object]:
        return {
            "index": self.index,
            "box": {
                "x": max(0.0, min(1.0, self.x / image_width)),
                "y": max(0.0, min(1.0, self.y / image_height)),
                "width": max(0.0, min(1.0, self.width / image_width)),
                "height": max(0.0, min(1.0, self.height / image_height)),
            },
            "detection_confidence": round(self.confidence, 4),
        }


class FaceService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._detector: object | None = None
        self._recognizer: object | None = None

    @property
    def models_ready(self) -> bool:
        return self.settings.yunet_model.is_file() and self.settings.sface_model.is_file()

    def warmup(self) -> None:
        self._load_models()

    def _load_models(self) -> tuple[object, object]:
        if not self.models_ready:
            raise FaceModelsUnavailableError(
                "Face models are unavailable. Rebuild the application image or restore app/models."
            )
        with self._lock:
            if self._detector is None:
                self._detector = cv2.FaceDetectorYN_create(
                    str(self.settings.yunet_model),
                    "",
                    (320, 320),
                    self.settings.face_detection_threshold,
                    0.3,
                    5000,
                )
            if self._recognizer is None:
                self._recognizer = cv2.FaceRecognizerSF_create(
                    str(self.settings.sface_model), ""
                )
        return self._detector, self._recognizer

    def decode(self, image_bytes: bytes) -> np.ndarray:
        if not image_bytes:
            raise InvalidImageError("The uploaded image is empty.")
        array = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None or image.ndim != 3:
            raise InvalidImageError("The uploaded file is not a valid image.")
        height, width = image.shape[:2]
        if width <= 0 or height <= 0 or width * height > self.settings.max_image_pixels:
            raise InvalidImageError("The decoded image dimensions are too large.")
        return image

    def detect(self, image: np.ndarray) -> list[DetectedFace]:
        detector, _ = self._load_models()
        height, width = image.shape[:2]
        with self._lock:
            detector.setInputSize((width, height))
            _, raw_faces = detector.detect(image)
        if raw_faces is None or len(raw_faces) == 0:
            return []
        ordered = sorted(raw_faces, key=lambda face: (float(face[1]), float(face[0])))
        return [
            DetectedFace(index=index, raw=np.asarray(face, dtype=np.float32))
            for index, face in enumerate(ordered)
        ]

    def detect_bytes(self, image_bytes: bytes) -> tuple[np.ndarray, list[DetectedFace]]:
        image = self.decode(image_bytes)
        return image, self.detect(image)

    def selected_face(
        self, image: np.ndarray, faces: list[DetectedFace], face_index: int
    ) -> DetectedFace:
        if not faces:
            raise NoFaceDetectedError("No face was detected in this image.")
        if face_index < 0 or face_index >= len(faces):
            raise FaceSelectionError("The selected face is no longer available.")
        return faces[face_index]

    def embedding(self, image: np.ndarray, face: DetectedFace) -> np.ndarray:
        _, recognizer = self._load_models()
        with self._lock:
            aligned = recognizer.alignCrop(image, face.raw)
            feature = recognizer.feature(aligned)
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm == 0:
            raise FacePipelineError("The selected face could not be encoded.")
        return vector / norm

    def best_similarity(self, image_bytes: bytes, query_embedding: np.ndarray) -> float | None:
        try:
            image, faces = self.detect_bytes(image_bytes)
        except FacePipelineError:
            return None
        if not faces:
            return None
        scores = [float(np.dot(query_embedding, self.embedding(image, face))) for face in faces]
        return max(scores, default=None)

    def crop(self, image: np.ndarray, face: DetectedFace, margin: float = 0.25) -> bytes:
        height, width = image.shape[:2]
        extra_x = face.width * margin
        extra_y = face.height * margin
        x1 = max(0, int(face.x - extra_x))
        y1 = max(0, int(face.y - extra_y))
        x2 = min(width, int(face.x + face.width + extra_x))
        y2 = min(height, int(face.y + face.height + extra_y))
        crop = image[y1:y2, x1:x2]
        ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise FacePipelineError("The selected face could not be prepared for search.")
        return encoded.tobytes()

    def thumbnail_data_url(self, image_bytes: bytes, max_side: int = 320) -> str:
        image = self.decode(image_bytes)
        height, width = image.shape[:2]
        scale = min(1.0, max_side / max(height, width))
        if scale < 1.0:
            image = cv2.resize(
                image,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if not ok:
            raise InvalidImageError("A candidate thumbnail could not be created.")
        value = base64.b64encode(encoded.tobytes()).decode("ascii")
        return f"data:image/jpeg;base64,{value}"
