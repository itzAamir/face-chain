import asyncio

from config import Settings
from services.search import CandidatePage, SearchService, classify_page, clean_title, normalize_url


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


def test_private_candidate_urls_are_rejected() -> None:
    service = SearchService(Settings(), face_service=object())
    assert asyncio.run(service._url_is_public("http://127.0.0.1/private.jpg")) is False
    assert asyncio.run(service._url_is_public("http://localhost/private.jpg")) is False
