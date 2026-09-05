"""Run the real face-to-social-post pipeline against a local image."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from config import settings
from services.face import FaceService
from services.search import SearchService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect a selected face, query configured live providers, verify candidates "
            "with SFace, and fail unless a real social-post URL is confirmed."
        )
    )
    parser.add_argument("image", type=Path, help="Path to a JPG, PNG, or WebP image")
    parser.add_argument("--face-index", type=int, default=None)
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    content = args.image.read_bytes()
    face_service = FaceService(settings)
    image, faces = face_service.detect_bytes(content)
    if not faces:
        print(json.dumps({"accepted": False, "reason": "no_face_detected"}, indent=2))
        return 2
    if args.face_index is None and len(faces) > 1:
        print(
            json.dumps(
                {
                    "accepted": False,
                    "reason": "face_selection_required",
                    "detected_faces": len(faces),
                },
                indent=2,
            )
        )
        return 2
    face_index = args.face_index if args.face_index is not None else 0
    selected = face_service.selected_face(image, faces, face_index)
    result = await SearchService(settings, face_service).search(content, image, selected)
    social_results = [
        {
            "platform": item["platform"],
            "page_title": item["page_title"],
            "page_url": item["page_url"],
            "face_similarity": item["face_similarity"],
            "discovery_provider": item["discovery_provider"],
        }
        for item in result["results"]
        if item["source_kind"] == "social_post"
    ]
    report = {
        "accepted": bool(social_results),
        "detected_faces": len(faces),
        "selected_face_index": face_index,
        "providers": result["providers"],
        "summary": result["summary"],
        "social_results": social_results,
    }
    print(json.dumps(report, indent=2))
    return 0 if social_results else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
