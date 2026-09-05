import asyncio
import hashlib

import httpx

from config import Settings
from services.search import (
    CandidatePage,
    PageImageParser,
    PageMetadata,
    SearchService,
    classify_page,
    classify_result_source,
    clean_text,
    clean_title,
    normalize_url,
    permission_denied_message,
)


def test_social_post_classification_requires_post_path() -> None:
    assert classify_page("https://www.instagram.com/p/ABC123/") == (
        "social_post",
        "Instagram",
    )
    assert classify_page("https://www.instagram.com/example/") == (
        "web_page",
        "instagram.com",
    )
    assert classify_page("https://x.com/example/status/12345") == (
        "social_post",
        "X",
    )
    assert classify_page("https://www.facebook.com/photo.php?fbid=123") == (
        "social_post",
        "Facebook",
    )
    assert classify_page("https://youtu.be/abc123") == ("social_post", "YouTube")


def test_lens_visual_match_keeps_social_post_classification() -> None:
    assert classify_result_source(
        "https://www.instagram.com/p/ABC123/",
        "https://cdn.example.com/person.jpg",
        "visual",
    ) == ("social_post", "Instagram")
    assert classify_result_source(
        "https://cdn.example.com/person.jpg",
        "https://cdn.example.com/person.jpg",
        "visual",
    ) == ("web_image", "cdn.example.com")


def test_url_and_title_normalization() -> None:
    assert normalize_url("https://Example.com/photo?a=1#fragment") == "https://example.com/photo?a=1"
    assert normalize_url("file:///etc/passwd") is None
    assert clean_title("<b> A &amp; B </b>") == "A & B"


def test_merge_pages_deduplicates_urls() -> None:
    one = CandidatePage("https://example.com/post", "Title", {"https://cdn.example/a.jpg"})
    two = CandidatePage(
        "https://example.com/post",
        "Title",
        partial_images={"https://cdn.example/b.jpg"},
    )
    merged = SearchService._merge_pages([[one], [two]])
    assert len(merged) == 1
    assert merged[0].full_images == {"https://cdn.example/a.jpg"}
    assert merged[0].partial_images == {"https://cdn.example/b.jpg"}


def test_merge_pages_preserves_visual_candidates() -> None:
    visual = CandidatePage(
        "https://cdn.example/person-two.jpg",
        "Visually similar public image",
        visual_images={"https://cdn.example/person-two.jpg"},
    )
    merged = SearchService._merge_pages([[visual]])
    assert merged[0].visual_images == {"https://cdn.example/person-two.jpg"}


def test_private_candidate_urls_are_rejected() -> None:
    service = SearchService(Settings(), face_service=object())
    assert asyncio.run(service._url_is_public("http://127.0.0.1/private.jpg")) is False
    assert asyncio.run(service._url_is_public("http://localhost/private.jpg")) is False


def test_permission_error_explains_billing_failure() -> None:
    error = RuntimeError("PERMISSION_DENIED reason: BILLING_DISABLED billing to be enabled")
    assert permission_denied_message(error) == (
        "Google Vision billing is not enabled for the configured project."
    )


def test_parse_serpapi_visual_matches() -> None:
    pages = SearchService._parse_serpapi_results(
        {
            "visual_matches": [
                {
                    "title": "A public post",
                    "link": "https://www.instagram.com/p/example/",
                    "image": "https://cdn.example.com/person.jpg",
                },
                {"title": "Missing image", "link": "https://example.com/page"},
            ]
        }
    )
    assert len(pages) == 1
    assert pages[0].page_url == "https://www.instagram.com/p/example/"
    assert pages[0].visual_images == {"https://cdn.example.com/person.jpg"}
    assert pages[0].discovery_providers == {"serpapi_google_lens:face"}


def test_parse_serpapi_exact_and_visual_matches() -> None:
    pages = SearchService._parse_serpapi_results(
        {
            "exact_matches": [
                {
                    "title": "Exact public post",
                    "link": "https://example.com/exact",
                    "thumbnail": "https://cdn.example.com/exact.jpg",
                }
            ],
            "visual_matches": [
                {
                    "title": "Different photo",
                    "link": "https://example.com/visual",
                    "image": "https://cdn.example.com/visual.jpg",
                }
            ],
        },
        "face",
    )
    assert len(pages) == 2
    assert pages[0].full_images == {"https://cdn.example.com/exact.jpg"}
    assert pages[1].visual_images == {"https://cdn.example.com/visual.jpg"}


def test_page_image_parser_collects_preview_and_json_ld() -> None:
    parser = PageImageParser()
    parser.feed(
        """
        <meta property="og:image" content="/preview.jpg">
        <meta name="twitter:image" content="https://cdn.example.com/card.jpg">
        <script type="application/ld+json">
          {"image": {"contentUrl": "https://cdn.example.com/schema.jpg"}}
        </script>
        """
    )
    assert parser.images == ["/preview.jpg", "https://cdn.example.com/card.jpg"]
    assert len(parser.json_ld) == 1


def test_page_parser_collects_post_text() -> None:
    parser = PageImageParser()
    parser.feed(
        """
        <meta property="og:title" content="Beach   day &amp; sunset">
        <meta property="og:description" content="  Posted from  Goa  ">
        <meta name="twitter:title" content="Ignored fallback">
        """
    )
    assert parser.title == "Beach day & sunset"
    assert parser.description == "Posted from Goa"


def test_page_parser_falls_back_to_twitter_text() -> None:
    parser = PageImageParser()
    parser.feed('<meta name="twitter:title" content="Card title">')
    assert parser.title == "Card title"
    assert parser.description is None


def test_page_parser_ignores_empty_text() -> None:
    parser = PageImageParser()
    parser.feed('<meta property="og:title" content="   "><meta name="twitter:title" content="">')
    assert parser.title is None


def test_clean_text_returns_none_when_nothing_remains() -> None:
    assert clean_text("<span>  </span>") is None
    assert clean_text("<b>hello</b>  world") == "hello world"
    assert clean_text("abcdef", limit=3) == "abc"
    # clean_title keeps its placeholder contract for provider-supplied titles.
    assert clean_title("<span>  </span>") == "Untitled public page"


def test_page_metadata_extracts_images_and_text() -> None:
    service = SearchService(Settings(), None)
    html_body = (
        '<meta property="og:image" content="/preview.jpg">'
        '<meta property="og:title" content="A post">'
        '<meta property="og:description" content="A caption">'
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=html_body, headers={"content-type": "text/html"}
        )

    async def run() -> PageMetadata:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await service._page_metadata(client, "https://example.com/p/1")

    metadata = asyncio.run(run())
    assert metadata.images == ["https://example.com/preview.jpg"]
    assert metadata.title == "A post"
    assert metadata.description == "A caption"


def test_page_metadata_is_empty_for_non_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8", headers={"content-type": "image/jpeg"})

    async def run() -> PageMetadata:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await SearchService(Settings(), None)._page_metadata(
                client, "https://example.com/photo.jpg"
            )

    metadata = asyncio.run(run())
    assert metadata.images == []
    assert metadata.title is None
    assert metadata.description is None


class _StubFace:
    index = 0


class _StubFaceService:
    """Confirms every candidate so the test exercises the fingerprint path only."""

    def embedding_variants(self, image, face):
        return object()

    def crop(self, image, face):
        return b"crop"

    def best_similarity(self, content, embeddings):
        return 0.91

    def thumbnail_data_url(self, content):
        return "data:image/jpeg;base64,AAAA"


def test_confirmed_result_carries_image_fingerprint_and_post_text(monkeypatch) -> None:
    settings = Settings(serpapi_api_key="test-key")
    service = SearchService(settings, _StubFaceService())
    image_body = b"the-exact-bytes-that-were-matched"

    async def fake_discover(crop, variant):
        return [
            CandidatePage(
                page_url="https://www.instagram.com/p/ABC123/",
                page_title="Provider supplied title",
                full_images={"https://cdn.example.com/post.jpg"},
                discovery_providers={"serpapi"},
            )
        ]

    async def fake_download(client, url):
        return image_body

    async def fake_metadata(client, url):
        return PageMetadata(title="Caption from the post", description="Shot in Goa")

    monkeypatch.setattr(service, "_serpapi_discover", fake_discover)
    monkeypatch.setattr(service, "_download", fake_download)
    monkeypatch.setattr(service, "_page_metadata", fake_metadata)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    payload = asyncio.run(service.search(b"upload", object(), _StubFace()))

    assert payload["status"] == "matched"
    [result] = payload["results"]
    assert result["image_url"] == "https://cdn.example.com/post.jpg"
    assert result["image_sha256"] == hashlib.sha256(image_body).hexdigest()
    assert result["image_bytes"] == len(image_body)
    assert result["og_title"] == "Caption from the post"
    assert result["og_description"] == "Shot in Goa"


def test_fingerprint_is_stable_across_runs(monkeypatch) -> None:
    """The same bytes must hash identically on every search."""
    settings = Settings(serpapi_api_key="test-key")
    image_body = b"stable-bytes"

    async def fake_discover(crop, variant):
        return [
            CandidatePage(
                page_url="https://x.com/someone/status/1",
                page_title="t",
                full_images={"https://cdn.example.com/a.jpg"},
                discovery_providers={"serpapi"},
            )
        ]

    async def fake_download(client, url):
        return image_body

    async def fake_metadata(client, url):
        return PageMetadata()

    digests = []
    for _ in range(2):
        service = SearchService(settings, _StubFaceService())
        monkeypatch.setattr(service, "_serpapi_discover", fake_discover)
        monkeypatch.setattr(service, "_download", fake_download)
        monkeypatch.setattr(service, "_page_metadata", fake_metadata)
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        payload = asyncio.run(service.search(b"upload", object(), _StubFace()))
        digests.append(payload["results"][0]["image_sha256"])

    assert digests[0] == digests[1] == hashlib.sha256(image_body).hexdigest()


def test_results_are_ranked_by_face_similarity_before_source_kind(monkeypatch) -> None:
    settings = Settings(serpapi_api_key="test-key")

    class ScoredFaceService(_StubFaceService):
        def best_similarity(self, content, embeddings):
            return {b"social": 0.72, b"web": 0.94}[content]

    service = SearchService(settings, ScoredFaceService())

    async def fake_discover(crop, variant):
        return [
            CandidatePage(
                page_url="https://www.instagram.com/p/ABC123/",
                page_title="Social result",
                full_images={"https://cdn.example.com/social.jpg"},
                discovery_providers={"serpapi"},
            ),
            CandidatePage(
                page_url="https://example.com/person",
                page_title="Web result",
                full_images={"https://cdn.example.com/web.jpg"},
                discovery_providers={"serpapi"},
            ),
        ]

    async def fake_download(client, url):
        return b"social" if url.endswith("social.jpg") else b"web"

    async def fake_metadata(client, url):
        return PageMetadata()

    monkeypatch.setattr(service, "_serpapi_discover", fake_discover)
    monkeypatch.setattr(service, "_download", fake_download)
    monkeypatch.setattr(service, "_page_metadata", fake_metadata)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    payload = asyncio.run(service.search(b"upload", object(), _StubFace()))

    assert [result["face_similarity"] for result in payload["results"]] == [0.94, 0.72]
