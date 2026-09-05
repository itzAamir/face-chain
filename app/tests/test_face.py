from pathlib import Path

import numpy as np

from config import Settings, settings
from services.face import FaceService, InvalidImageError


class FakeDetector:
    def setInputSize(self, _: tuple[int, int]) -> None:
        pass

    def detect(self, _: np.ndarray) -> tuple[None, np.ndarray]:
        # The detector deliberately returns bottom-right before top-left.
        return None, np.array(
            [
                [60, 50, 20, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.96],
                [10, 10, 30, 30, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.98],
            ],
            dtype=np.float32,
        )


def configured_service(tmp_path: Path) -> FaceService:
    yunet = tmp_path / "yunet.onnx"
    sface = tmp_path / "sface.onnx"
    yunet.touch()
    sface.touch()
    service = FaceService(Settings(yunet_model=yunet, sface_model=sface))
    service._detector = FakeDetector()
    service._recognizer = object()
    return service


def test_detect_orders_faces_top_to_bottom_then_left_to_right(tmp_path: Path) -> None:
    service = configured_service(tmp_path)
    faces = service.detect(np.zeros((100, 100, 3), dtype=np.uint8))

    assert [face.index for face in faces] == [0, 1]
    assert [(face.x, face.y) for face in faces] == [(10.0, 10.0), (60.0, 50.0)]
    assert faces[0].as_api_dict(100, 100)["box"] == {
        "x": 0.1,
        "y": 0.1,
        "width": 0.3,
        "height": 0.3,
    }


def test_decode_rejects_non_image_bytes(tmp_path: Path) -> None:
    service = configured_service(tmp_path)
    try:
        service.decode(b"not an image")
    except InvalidImageError as exc:
        assert "not a valid image" in str(exc)
    else:
        raise AssertionError("Invalid image bytes should be rejected")


def test_bundled_models_load_and_run_blank_inference() -> None:
    service = FaceService(settings)
    service.warmup()
    assert service.models_ready is True
    assert service.detect(np.zeros((480, 640, 3), dtype=np.uint8)) == []


def test_provider_upload_jpeg_respects_size_limit(tmp_path: Path) -> None:
    service = configured_service(tmp_path)
    image = np.random.default_rng(7).integers(0, 255, (900, 1200, 3), dtype=np.uint8)
    import cv2

    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    bounded = service.jpeg_within_limit(encoded.tobytes(), max_bytes=100_000)
    assert len(bounded) <= 100_000
