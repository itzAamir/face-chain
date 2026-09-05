import asyncio

from config import Settings
from services.search import (
    CandidatePage,
    PageImageParser,
    SearchService,
    classify_page,
    classify_result_source,
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
