"""Google Web Detection plus local SFace candidate confirmation."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from config import Settings
from services.face import DetectedFace, FaceService, InvalidImageError


class SearchConfigurationError(RuntimeError):
    pass


class SearchRateLimitError(RuntimeError):
    pass


class SearchProviderError(RuntimeError):
    pass


@dataclass
class CandidatePage:
    page_url: str
    page_title: str
    full_images: set[str] = field(default_factory=set)
    partial_images: set[str] = field(default_factory=set)


SOCIAL_POST_RULES: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    ("Instagram", "instagram.com", (re.compile(r"^/(?:p|reel)/"),)),
    ("X", "x.com", (re.compile(r"^/[^/]+/status/\d+"),)),
    ("X", "twitter.com", (re.compile(r"^/[^/]+/status/\d+"),)),
    ("Facebook", "facebook.com", (re.compile(r"/(?:posts|photos|permalink)"), re.compile(r"[?&]story_fbid="))),
    ("Reddit", "reddit.com", (re.compile(r"^/r/[^/]+/comments/"),)),
    ("LinkedIn", "linkedin.com", (re.compile(r"/(?:posts/|feed/update/)"),)),
    ("TikTok", "tiktok.com", (re.compile(r"^/@[^/]+/video/\d+"),)),
    ("YouTube", "youtube.com", (re.compile(r"^/(?:watch|shorts/)"),)),
    ("Threads", "threads.net", (re.compile(r"^/@[^/]+/post/"),)),
    ("Pinterest", "pinterest.com", (re.compile(r"^/pin/"),)),
)


def normalize_url(value: str) -> str | None:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return None
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), host + port, path, "", parsed.query, ""))


def classify_page(page_url: str) -> tuple[str, str]:
    parsed = urlparse(page_url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    searchable_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    for platform, domain, patterns in SOCIAL_POST_RULES:
        if (host == domain or host.endswith(f".{domain}")) and any(
            pattern.search(searchable_path) for pattern in patterns
        ):
            return "social_post", platform
    return "web_page", host or "Public web"


def clean_title(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return " ".join(without_tags.split())[:200] or "Untitled public page"


class SearchService:
    def __init__(self, settings: Settings, face_service: FaceService) -> None:
        self.settings = settings
        self.face_service = face_service
        self._vision_client: object | None = None

    def _client(self) -> object:
        if self._vision_client is not None:
            return self._vision_client
        try:
            from google.cloud import vision

            self._vision_client = vision.ImageAnnotatorClient()
        except Exception as exc:
            raise SearchConfigurationError(
                "Google Web Detection credentials are not configured correctly."
            ) from exc
        return self._vision_client

    def _web_detection(self, content: bytes) -> list[CandidatePage]:
        try:
            from google.api_core import exceptions as google_exceptions
            from google.cloud import vision

            response = self._client().web_detection(
                image=vision.Image(content=content),
                timeout=self.settings.provider_timeout_seconds,
            )
            if response.error.message:
                raise SearchProviderError(response.error.message)
        except Exception as exc:
            if "google_exceptions" in locals() and isinstance(
                exc, google_exceptions.ResourceExhausted
            ):
                raise SearchRateLimitError("The search provider rate limit was reached.") from exc
            if isinstance(exc, (SearchConfigurationError, SearchProviderError)):
                raise
            if "google_exceptions" in locals() and isinstance(
                exc,
                (
                    google_exceptions.Unauthenticated,
                    google_exceptions.PermissionDenied,
                ),
            ):
                raise SearchConfigurationError(
                    "Google Web Detection credentials were rejected."
                ) from exc
            raise SearchProviderError("Google Web Detection could not complete the search.") from exc

        pages: list[CandidatePage] = []
        web = response.web_detection
        for page in web.pages_with_matching_images or []:
            page_url = normalize_url(page.url)
            if not page_url:
                continue
            candidate = CandidatePage(page_url=page_url, page_title=clean_title(page.page_title))
            candidate.full_images.update(
                normalized
                for item in page.full_matching_images or []
                if (normalized := normalize_url(item.url))
            )
            candidate.partial_images.update(
                normalized
                for item in page.partial_matching_images or []
                if (normalized := normalize_url(item.url))
            )
            if candidate.full_images or candidate.partial_images:
                pages.append(candidate)
        return pages

    @staticmethod
    def _merge_pages(groups: list[list[CandidatePage]]) -> list[CandidatePage]:
        merged: dict[str, CandidatePage] = {}
        for pages in groups:
            for page in pages:
                current = merged.setdefault(
                    page.page_url,
                    CandidatePage(page_url=page.page_url, page_title=page.page_title),
                )
                if current.page_title == "Untitled public page":
                    current.page_title = page.page_title
                current.full_images.update(page.full_images)
                current.partial_images.update(page.partial_images)
        return list(merged.values())

    async def _url_is_public(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host or host.lower() == "localhost":
            return False
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
            )
        except OSError:
            return False
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address[4][0])
            except ValueError:
                return False
            if not ip.is_global:
                return False
        return True

    async def _download(self, client: httpx.AsyncClient, initial_url: str) -> bytes | None:
        url = initial_url
        for redirect_number in range(self.settings.max_redirects + 1):
            if not await self._url_is_public(url):
                return None
            try:
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        if redirect_number >= self.settings.max_redirects:
                            return None
                        location = response.headers.get("location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        return None
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if not content_type.startswith("image/"):
                        return None
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > self.settings.max_candidate_bytes:
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.max_candidate_bytes:
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
            except (httpx.HTTPError, ValueError):
                return None
        return None

    async def search(
        self,
        image_bytes: bytes,
        image: object,
        selected_face: DetectedFace,
    ) -> dict[str, object]:
        query_embedding = await asyncio.to_thread(
            self.face_service.embedding, image, selected_face
        )
        face_crop = await asyncio.to_thread(self.face_service.crop, image, selected_face)
        original_pages, crop_pages = await asyncio.gather(
            asyncio.to_thread(self._web_detection, image_bytes),
            asyncio.to_thread(self._web_detection, face_crop),
        )
        pages = self._merge_pages([original_pages, crop_pages])

        work: list[tuple[CandidatePage, str, str]] = []
        seen_page_images: set[tuple[str, str]] = set()
        for page in pages:
            for match_type, urls in (("full", page.full_images), ("partial", page.partial_images)):
                for image_url in sorted(urls):
                    page_image = (page.page_url, image_url)
                    if page_image in seen_page_images:
                        continue
                    seen_page_images.add(page_image)
                    work.append((page, image_url, match_type))
                    if len(work) >= self.settings.candidate_limit:
                        break
                if len(work) >= self.settings.candidate_limit:
                    break
            if len(work) >= self.settings.candidate_limit:
                break

        limits = httpx.Limits(
            max_connections=self.settings.download_concurrency,
            max_keepalive_connections=self.settings.download_concurrency,
        )
        timeout = httpx.Timeout(self.settings.download_timeout_seconds)
        semaphore = asyncio.Semaphore(self.settings.download_concurrency)
        results: list[dict[str, object]] = []

        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            headers={"User-Agent": "FaceChainVerifier/0.2 (+local demo)"},
        ) as client:
            async def inspect(item: tuple[CandidatePage, str, str]) -> dict[str, object] | None:
                page, image_url, match_type = item
                async with semaphore:
                    content = await self._download(client, image_url)
                if not content:
                    return None
                similarity = await asyncio.to_thread(
                    self.face_service.best_similarity, content, query_embedding
                )
                if similarity is None or similarity < self.settings.face_match_threshold:
                    return None
                try:
                    thumbnail = await asyncio.to_thread(
                        self.face_service.thumbnail_data_url, content
                    )
                except InvalidImageError:
                    return None
                source_kind, platform = classify_page(page.page_url)
                return {
                    "source_kind": source_kind,
                    "platform": platform,
                    "page_title": page.page_title,
                    "page_url": page.page_url,
                    "provider_match_type": match_type,
                    "face_similarity": round(similarity, 4),
                    "thumbnail_data_url": thumbnail,
                }

            inspected = await asyncio.gather(*(inspect(item) for item in work))
            confirmed = [result for result in inspected if result is not None]

        # A page can expose several matching image URLs. Keep its strongest confirmation.
        by_page: dict[str, dict[str, object]] = {}
        for result in confirmed:
            page_url = str(result["page_url"])
            current = by_page.get(page_url)
            if current is None or (
                result["provider_match_type"] == "full",
                float(result["face_similarity"]),
            ) > (
                current["provider_match_type"] == "full",
                float(current["face_similarity"]),
            ):
                by_page[page_url] = result
        results = list(by_page.values())

        results.sort(
            key=lambda item: (
                item["source_kind"] != "social_post",
                item["provider_match_type"] != "full",
                -float(item["face_similarity"]),
            )
        )
        results = results[: self.settings.result_limit]
        social_count = sum(result["source_kind"] == "social_post" for result in results)
        return {
            "status": "matched" if social_count else "no_match",
            "provider": "google_web_detection",
            "query_face": {"index": selected_face.index},
            "summary": {
                "candidates_examined": len(work),
                "confirmed_results": len(results),
                "confirmed_social_posts": social_count,
            },
            "results": results,
        }
