"""Google Web Detection plus local SFace candidate confirmation."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
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
    visual_images: set[str] = field(default_factory=set)
    discovery_providers: set[str] = field(default_factory=set)


@dataclass
class PageMetadata:
    """Public preview data lifted from a candidate page."""

    images: list[str] = field(default_factory=list)
    title: str | None = None
    description: str | None = None


class PageImageParser(HTMLParser):
    """Collect public preview images and text without executing page scripts."""

    META_KEYS = {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}
    TITLE_KEYS = ("og:title", "twitter:title")
    DESCRIPTION_KEYS = ("og:description", "twitter:description")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.json_ld: list[str] = []
        self.titles: dict[str, str] = {}
        self.descriptions: dict[str, str] = {}
        self._in_json_ld = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if key in self.META_KEYS and content:
                self.images.append(content)
            elif key in self.TITLE_KEYS and content and key not in self.titles:
                self.titles[key] = content
            elif key in self.DESCRIPTION_KEYS and content and key not in self.descriptions:
                self.descriptions[key] = content
        elif tag.lower() == "script" and values.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._script_parts))
            self._in_json_ld = False
            self._script_parts = []

    @staticmethod
    def _preferred(
        values: dict[str, str], keys: tuple[str, ...], limit: int
    ) -> str | None:
        for key in keys:
            text = clean_text(values.get(key) or "", limit)
            if text:
                return text
        return None

    @property
    def title(self) -> str | None:
        return self._preferred(self.titles, self.TITLE_KEYS, 200)

    @property
    def description(self) -> str | None:
        return self._preferred(self.descriptions, self.DESCRIPTION_KEYS, 500)


def _json_image_values(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"image", "imageurl", "thumbnailurl", "contenturl"}:
                if isinstance(child, str):
                    found.append(child)
                else:
                    found.extend(_json_image_values(child))
            elif isinstance(child, (dict, list)):
                found.extend(_json_image_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_json_image_values(child))
    return found


SOCIAL_POST_RULES: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    ("Instagram", "instagram.com", (re.compile(r"^/(?:p|reel|tv)/"),)),
    ("X", "x.com", (re.compile(r"^/[^/]+/status/\d+"),)),
    ("X", "twitter.com", (re.compile(r"^/[^/]+/status/\d+"),)),
    ("Facebook", "facebook.com", (
        re.compile(r"/(?:posts|photos|permalink|reel|share/p)/"),
        re.compile(r"/(?:photo|story)\.php"),
        re.compile(r"[?&](?:story_fbid|fbid)="),
    )),
    ("Facebook", "fb.watch", (re.compile(r"^/[^/]+"),)),
    ("Reddit", "reddit.com", (re.compile(r"^/(?:r/[^/]+/)?comments/"),)),
    ("LinkedIn", "linkedin.com", (re.compile(r"/(?:posts/|feed/update/|pulse/)"),)),
    ("TikTok", "tiktok.com", (re.compile(r"^/@[^/]+/video/\d+"),)),
    ("YouTube", "youtube.com", (re.compile(r"^/(?:watch|shorts/|live/)"),)),
    ("YouTube", "youtu.be", (re.compile(r"^/[^/]+"),)),
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


def classify_result_source(
    page_url: str, image_url: str, match_type: str
) -> tuple[str, str]:
    source_kind, platform = classify_page(page_url)
    # Google Vision visual matches may be bare image URLs. Lens visual matches,
    # however, often point at real posts and must keep their social classification.
    if match_type == "visual" and normalize_url(page_url) == normalize_url(image_url):
        source_kind = "web_image"
    return source_kind, platform


def clean_text(value: str, limit: int = 200) -> str | None:
    """Collapse markup and whitespace, returning None when nothing is left."""
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return " ".join(without_tags.split())[:limit] or None


def clean_title(value: str) -> str:
    return clean_text(value) or "Untitled public page"


def permission_denied_message(error: Exception) -> str:
    details = str(error).lower()
    if "billing_disabled" in details or "billing to be enabled" in details:
        return "Google Vision billing is not enabled for the configured project."
    if "service_disabled" in details or "api has not been used" in details:
        return "The Google Cloud Vision API is not enabled for the configured project."
    return "Google Web Detection does not permit this service account to run searches."


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
                raise SearchConfigurationError(permission_denied_message(exc)) from exc
            raise SearchProviderError("Google Web Detection could not complete the search.") from exc

        pages: list[CandidatePage] = []
        web = response.web_detection
        for page in web.pages_with_matching_images or []:
            page_url = normalize_url(page.url)
            if not page_url:
                continue
            candidate = CandidatePage(page_url=page_url, page_title=clean_title(page.page_title))
            candidate.discovery_providers.add("google_web_detection")
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

        # Google associates source pages only with full/partial matches. Visual
        # matches still provide public image URLs, so retain those as direct-image
        # candidates and let SFace decide whether the selected person is present.
        for item in web.visually_similar_images or []:
            image_url = normalize_url(item.url)
            if not image_url:
                continue
            pages.append(
                CandidatePage(
                    page_url=image_url,
                    page_title="Visually similar public image",
                    visual_images={image_url},
                    discovery_providers={"google_web_detection"},
                )
            )
        return pages

    @staticmethod
    def _parse_serpapi_results(
        payload: dict[str, object], query_variant: str = "face"
    ) -> list[CandidatePage]:
        pages: list[CandidatePage] = []
        for result_key, match_type in (
            ("exact_matches", "full"),
            ("visual_matches", "visual"),
        ):
            matches = payload.get(result_key, [])
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                page_url = normalize_url(str(match.get("link", "")))
                image_url = normalize_url(
                    str(match.get("image") or match.get("thumbnail") or "")
                )
                if not page_url or not image_url:
                    continue
                candidate = CandidatePage(
                    page_url=page_url,
                    page_title=clean_title(str(match.get("title", ""))),
                    discovery_providers={f"serpapi_google_lens:{query_variant}"},
                )
                if match_type == "full":
                    candidate.full_images.add(image_url)
                else:
                    candidate.visual_images.add(image_url)
                pages.append(candidate)
        return pages

    async def _serpapi_discover(
        self, query_image: bytes, query_variant: str
    ) -> list[CandidatePage]:
        api_key = self.settings.serpapi_api_key
        if not api_key:
            raise SearchConfigurationError("SERPAPI_API_KEY is not configured.")
        upload_content = await asyncio.to_thread(
            self.face_service.jpeg_within_limit, query_image
        )
        timeout = httpx.Timeout(self.settings.serpapi_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                upload_response = await client.post(
                    "https://serpapi.com/image",
                    data={"api_key": api_key},
                    files={
                        "image": (
                            f"selected-face-{query_variant}.jpg",
                            upload_content,
                            "image/jpeg",
                        )
                    },
                )
                if upload_response.status_code == 429:
                    raise SearchRateLimitError("The SerpAPI search allowance was reached.")
                if upload_response.status_code in {401, 403}:
                    raise SearchConfigurationError("The SerpAPI key was rejected.")
                upload_response.raise_for_status()
                upload_payload = upload_response.json()
                if not isinstance(upload_payload, dict):
                    raise SearchProviderError("SerpAPI returned an invalid upload response.")
                if upload_payload.get("error"):
                    raise SearchProviderError("SerpAPI could not accept the selected face crop.")
                image_id = upload_payload.get("image_id")
                if not image_id:
                    raise SearchProviderError("SerpAPI did not return an image identifier.")

                search_response = await client.get(
                    "https://serpapi.com/search.json",
                    params={
                        "engine": "google_lens",
                        "image_id": image_id,
                        "type": "all",
                        "safe": "active",
                        "auto_crop": "false",
                        "hl": "en",
                        "api_key": api_key,
                    },
                )
                if search_response.status_code == 429:
                    raise SearchRateLimitError("The SerpAPI search allowance was reached.")
                if search_response.status_code in {401, 403}:
                    raise SearchConfigurationError("The SerpAPI key was rejected.")
                search_response.raise_for_status()
                payload = search_response.json()
                if not isinstance(payload, dict):
                    raise SearchProviderError("SerpAPI returned an invalid search response.")
                error = payload.get("error")
                if error:
                    lowered = str(error).lower()
                    if "credit" in lowered or "rate" in lowered or "limit" in lowered:
                        raise SearchRateLimitError("The SerpAPI search allowance was reached.")
                    raise SearchProviderError("Google Lens could not complete the visual search.")
                return self._parse_serpapi_results(payload, query_variant)
        except (SearchConfigurationError, SearchRateLimitError, SearchProviderError):
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchProviderError("SerpAPI Google Lens could not complete the search.") from exc

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
                current.visual_images.update(page.visual_images)
                current.discovery_providers.update(page.discovery_providers)
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

    async def fetch_public_image(self, url: str) -> bytes | None:
        """Download one public image using the same guards as candidate inspection."""
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.download_timeout_seconds),
            limits=limits,
            follow_redirects=False,
            headers={"User-Agent": "FaceChainVerifier/0.2 (+local demo)"},
        ) as client:
            return await self._download(client, url)

    async def _page_metadata(
        self, client: httpx.AsyncClient, initial_url: str
    ) -> PageMetadata:
        """Fetch bounded HTML and extract preview images and text without running JavaScript."""
        empty = PageMetadata()
        url = initial_url
        for redirect_number in range(self.settings.max_redirects + 1):
            if not await self._url_is_public(url):
                return empty
            try:
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        if redirect_number >= self.settings.max_redirects:
                            return empty
                        location = response.headers.get("location")
                        if not location:
                            return empty
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        return empty
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        return empty
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > self.settings.max_page_bytes:
                        return empty
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.max_page_bytes:
                            return empty
                        chunks.append(chunk)
                    raw_html = b"".join(chunks).decode("utf-8", errors="replace")
            except (httpx.HTTPError, ValueError):
                return empty

            parser = PageImageParser()
            try:
                parser.feed(raw_html)
            except (ValueError, TypeError):
                return empty
            values = list(parser.images)
            for raw_json in parser.json_ld:
                try:
                    values.extend(_json_image_values(json.loads(raw_json)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

            normalized: list[str] = []
            seen: set[str] = set()
            for value in values:
                candidate = normalize_url(urljoin(url, html.unescape(value)))
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    normalized.append(candidate)
                if len(normalized) >= self.settings.per_page_image_limit:
                    break
            return PageMetadata(
                images=normalized,
                title=parser.title,
                description=parser.description,
            )
        return empty

    async def search(
        self,
        image_bytes: bytes,
        image: object,
        selected_face: DetectedFace,
    ) -> dict[str, object]:
        query_embeddings = await asyncio.to_thread(
            self.face_service.embedding_variants, image, selected_face
        )
        face_crop = await asyncio.to_thread(self.face_service.crop, image, selected_face)
        discovery_jobs: list[tuple[str, object]] = []
        google_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if google_credentials:
            async def google_discovery() -> list[CandidatePage]:
                original_pages, crop_pages = await asyncio.gather(
                    asyncio.to_thread(self._web_detection, image_bytes),
                    asyncio.to_thread(self._web_detection, face_crop),
                )
                return self._merge_pages([original_pages, crop_pages])

            discovery_jobs.append(("google_web_detection", google_discovery()))
        if self.settings.serpapi_api_key:
            discovery_jobs.append(
                ("serpapi_google_lens", self._serpapi_discover(face_crop, "face"))
            )
        if not discovery_jobs:
            raise SearchConfigurationError(
                "Configure Google Vision credentials or SERPAPI_API_KEY to search."
            )

        discovered = await asyncio.gather(
            *(job for _, job in discovery_jobs), return_exceptions=True
        )
        successful_groups: list[list[CandidatePage]] = []
        successful_providers: list[str] = []
        errors: list[Exception] = []
        for (provider, _), outcome in zip(discovery_jobs, discovered):
            if isinstance(outcome, Exception):
                errors.append(outcome)
            else:
                successful_groups.append(outcome)
                successful_providers.append(provider)
        if not successful_groups:
            for error_type in (
                SearchRateLimitError,
                SearchConfigurationError,
                SearchProviderError,
            ):
                matching = next((error for error in errors if isinstance(error, error_type)), None)
                if matching:
                    raise matching
            raise SearchProviderError("No search provider could complete the request.")
        pages = self._merge_pages(successful_groups)

        exact_work: list[tuple[CandidatePage, str, str]] = []
        visual_work: list[tuple[CandidatePage, str, str]] = []
        seen_page_images: set[tuple[str, str]] = set()
        for page in pages:
            for match_type, urls in (
                ("full", page.full_images),
                ("partial", page.partial_images),
                ("visual", page.visual_images),
            ):
                for image_url in sorted(urls):
                    page_image = (page.page_url, image_url)
                    if page_image in seen_page_images:
                        continue
                    seen_page_images.add(page_image)
                    target = visual_work if match_type == "visual" else exact_work
                    target.append((page, image_url, match_type))

        def work_priority(item: tuple[CandidatePage, str, str]) -> tuple[bool, str]:
            page, _, _ = item
            return (classify_page(page.page_url)[0] != "social_post", page.page_url)

        exact_work.sort(key=work_priority)
        visual_work.sort(key=work_priority)

        if visual_work:
            visual_budget = max(1, self.settings.candidate_limit // 2)
            exact_budget = self.settings.candidate_limit - visual_budget
            work = exact_work[:exact_budget] + visual_work[:visual_budget]
            if len(work) < self.settings.candidate_limit:
                remaining_exact = exact_work[exact_budget:]
                remaining_visual = visual_work[visual_budget:]
                work.extend((remaining_exact + remaining_visual)[: self.settings.candidate_limit - len(work)])
        else:
            work = exact_work[: self.settings.candidate_limit]

        limits = httpx.Limits(
            max_connections=self.settings.download_concurrency,
            max_keepalive_connections=self.settings.download_concurrency,
        )
        timeout = httpx.Timeout(self.settings.download_timeout_seconds)
        semaphore = asyncio.Semaphore(self.settings.download_concurrency)
        results: list[dict[str, object]] = []
        page_metadata_tasks: dict[str, asyncio.Task[PageMetadata]] = {}
        images_examined = 0

        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            headers={"User-Agent": "FaceChainVerifier/0.2 (+local demo)"},
        ) as client:
            async def page_metadata(page_url: str, original_image_url: str) -> PageMetadata:
                """Fetch a candidate page at most once, shared by every inspector."""
                if normalize_url(page_url) == normalize_url(original_image_url):
                    return PageMetadata()
                task = page_metadata_tasks.get(page_url)
                if task is None:
                    task = asyncio.create_task(self._page_metadata(client, page_url))
                    page_metadata_tasks[page_url] = task
                return await task

            async def page_images(page_url: str, original_image_url: str) -> list[str]:
                return (await page_metadata(page_url, original_image_url)).images

            async def inspect(item: tuple[CandidatePage, str, str]) -> dict[str, object] | None:
                nonlocal images_examined
                page, image_url, match_type = item
                candidate_urls = [image_url]
                seen_images: set[str] = set()
                fallback_loaded = False
                position = 0
                while position < len(candidate_urls) or not fallback_loaded:
                    if position >= len(candidate_urls):
                        candidate_urls.extend(
                            await page_images(page.page_url, image_url)
                        )
                        fallback_loaded = True
                        continue
                    candidate_url = candidate_urls[position]
                    position += 1
                    if candidate_url in seen_images:
                        continue
                    seen_images.add(candidate_url)
                    async with semaphore:
                        content = await self._download(client, candidate_url)
                    if not content:
                        continue
                    images_examined += 1
                    similarity = await asyncio.to_thread(
                        self.face_service.best_similarity, content, query_embeddings
                    )
                    if similarity is None or similarity < self.settings.face_match_threshold:
                        continue
                    try:
                        thumbnail = await asyncio.to_thread(
                            self.face_service.thumbnail_data_url, content
                        )
                    except InvalidImageError:
                        continue
                    source_kind, platform = classify_result_source(
                        page.page_url, candidate_url, match_type
                    )
                    confirmation: dict[str, object] = {
                        "source_kind": source_kind,
                        "platform": platform,
                        "page_title": page.page_title,
                        "page_url": page.page_url,
                        "image_url": candidate_url,
                        "image_sha256": hashlib.sha256(content).hexdigest(),
                        "image_bytes": len(content),
                        "provider_match_type": match_type,
                        "face_similarity": round(similarity, 4),
                        "thumbnail_data_url": thumbnail,
                        "discovery_provider": "+".join(sorted(page.discovery_providers)),
                    }
                    if self.settings.evidence_store_images:
                        # Consumed and removed by EvidenceService; it must never
                        # reach the API response.
                        confirmation["_image_content"] = content
                    return confirmation
                return None

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
                    -float(item["face_similarity"]),
                    item["source_kind"] != "social_post",
                    item["provider_match_type"] == "visual",
                    item["provider_match_type"] != "full",
                    str(item["page_url"]),
                )
            )
            results = results[: self.settings.result_limit]

            # Only the results that survive ranking are fingerprinted, so the post's
            # own text is fetched at most `result_limit` times and usually comes
            # from a page already in the cache.
            async def attach_post_text(result: dict[str, object]) -> None:
                metadata = await page_metadata(
                    str(result["page_url"]), str(result["image_url"])
                )
                result["og_title"] = metadata.title
                result["og_description"] = metadata.description

            await asyncio.gather(*(attach_post_text(result) for result in results))
        social_count = sum(result["source_kind"] == "social_post" for result in results)
        return {
            "status": "matched" if social_count else "no_match",
            "providers": successful_providers,
            "query_face": {"index": selected_face.index},
            "summary": {
                "candidates_examined": len(work),
                "candidate_images_examined": images_examined,
                "confirmed_results": len(results),
                "confirmed_social_posts": social_count,
            },
            "results": results,
        }
