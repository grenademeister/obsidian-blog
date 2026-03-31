import sys
import textwrap
from datetime import datetime
from pathlib import Path

import os
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from main import Settings, create_app, get_cached_posts, load_posts, reset_post_cache


def make_app(tmp_path: Path) -> TestClient:
    app = create_app(Settings(vault_dir=tmp_path, cors_origins=["https://grenademeister.github.io"]))
    return TestClient(app)


def write_note(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def set_mtime(path: Path, timestamp: datetime) -> None:
    unix_time = timestamp.timestamp()
    os.utime(path, (unix_time, unix_time))


@pytest.fixture(autouse=True)
def clear_post_cache() -> None:
    reset_post_cache()
    yield
    reset_post_cache()


def test_health() -> None:
    client = make_app(Path("stub_vault"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_stub_vault_has_multiple_published_posts_and_hides_private_notes() -> None:
    client = make_app(Path("stub_vault"))
    response = client.get("/posts")

    assert response.status_code == 200
    slugs = [post["slug"] for post in response.json()]
    assert "private-note" not in slugs
    assert "hello-world" in slugs
    assert "with-frontmatter" in slugs
    assert "deep-note" in slugs


def test_posts_only_include_published_notes(tmp_path: Path) -> None:
    write_note(
        tmp_path / "published.md",
        """
        #publish #ai

        Published body.
        """,
    )
    write_note(tmp_path / "private.md", "Private body.")

    client = make_app(tmp_path)
    response = client.get("/posts")

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["published"]


def test_posts_are_sorted_by_date_descending_with_undated_last(tmp_path: Path) -> None:
    write_note(
        tmp_path / "older.md",
        """
        ---
        date: 2026-03-01
        ---

        #publish

        Older.
        """,
    )
    write_note(
        tmp_path / "newer.md",
        """
        ---
        date: 2026-03-30
        ---

        #publish

        Newer.
        """,
    )
    write_note(
        tmp_path / "undated.md",
        """
        #publish

        No date here.
        """,
    )
    set_mtime(tmp_path / "undated.md", datetime(2026, 2, 1, 10, 0, 0))

    posts = load_posts(tmp_path)

    assert [post.slug for post in posts] == ["newer", "older", "undated"]


def test_invalid_dates_fall_back_to_file_time(tmp_path: Path) -> None:
    write_note(
        tmp_path / "dated.md",
        """
        ---
        date: 2026-03-30
        ---

        #publish

        Dated.
        """,
    )
    write_note(
        tmp_path / "invalid-date.md",
        """
        ---
        date: not-a-date
        ---

        #publish

        Invalid date.
        """,
    )
    set_mtime(tmp_path / "invalid-date.md", datetime(2026, 3, 15, 10, 0, 0))

    posts = load_posts(tmp_path)

    assert [(post.slug, post.date) for post in posts] == [("dated", "2026-03-30"), ("invalid-date", "2026-03-15")]


def test_missing_dates_fall_back_to_file_time(tmp_path: Path) -> None:
    write_note(
        tmp_path / "no-frontmatter-date.md",
        """
        #publish

        Body without explicit date.
        """,
    )
    set_mtime(tmp_path / "no-frontmatter-date.md", datetime(2026, 1, 5, 9, 0, 0))

    client = make_app(tmp_path)
    response = client.get("/posts/no-frontmatter-date")

    assert response.status_code == 200
    assert response.json()["date"] == "2026-01-05"


def test_summary_and_tags_fallbacks_with_filename_title(tmp_path: Path) -> None:
    write_note(
        tmp_path / "fallbacks.md",
        """
        #publish #ml #notes

        # Derived Title

        First paragraph becomes the summary.

        Second paragraph.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/fallbacks")

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "fallbacks"
    assert payload["summary"] == "First paragraph becomes the summary."
    assert payload["tags"] == ["ml", "notes"]


def test_title_falls_back_to_slug_when_no_frontmatter_or_h1_exists(tmp_path: Path) -> None:
    write_note(
        tmp_path / "slug-title.md",
        """
        #publish

        Plain body without a heading.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/slug-title")

    assert response.status_code == 200
    assert response.json()["title"] == "slug-title"


def test_summary_is_truncated_for_long_first_paragraph(tmp_path: Path) -> None:
    long_text = "A" * 220
    write_note(
        tmp_path / "long-summary.md",
        f"""
        #publish

        {long_text}
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/long-summary")

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert len(summary) == 180
    assert summary.endswith("...")


def test_frontmatter_summary_does_not_override_filename_title(tmp_path: Path) -> None:
    write_note(
        tmp_path / "frontmatter.md",
        """
        ---
        title: Explicit Title
        summary: Explicit summary
        tags:
          - ai
        date: 2026-03-30
        ---

        #publish

        # Ignored Heading

        Body paragraph.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/frontmatter")

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "frontmatter"
    assert payload["summary"] == "Explicit summary"
    assert payload["tags"] == ["ai"]
    assert payload["date"] == "2026-03-30"


def test_tags_are_deduplicated_and_publish_is_hidden_from_response(tmp_path: Path) -> None:
    write_note(
        tmp_path / "dedupe-tags.md",
        """
        ---
        tags:
          - AI
          - "#publish"
          - ai
          - Notes
        ---

        #publish #notes #AI

        Content.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/dedupe-tags")

    assert response.status_code == 200
    assert response.json()["tags"] == ["ai", "notes"]


def test_post_detail_renders_html(tmp_path: Path) -> None:
    write_note(
        tmp_path / "rendered.md",
        """
        #publish

        # Heading

        Hello **world**.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/rendered")

    assert response.status_code == 200
    assert "<strong>world</strong>" in response.json()["html"]


def test_tag_only_lines_are_not_rendered_into_html(tmp_path: Path) -> None:
    write_note(
        tmp_path / "tag-lines.md",
        """
        #publish #ai

        Actual body paragraph.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/tag-lines")

    assert response.status_code == 200
    html = response.json()["html"]
    assert "Actual body paragraph." in html
    assert "#publish" not in html
    assert "#ai" not in html


def test_loader_reads_nested_markdown_files(tmp_path: Path) -> None:
    write_note(
        tmp_path / "nested" / "path" / "deep-post.md",
        """
        #publish

        Deep content.
        """,
    )

    posts = load_posts(tmp_path)

    assert [post.slug for post in posts] == ["deep-post"]


def test_invalid_frontmatter_file_is_skipped_without_breaking_other_posts(tmp_path: Path) -> None:
    write_note(
        tmp_path / "broken.md",
        """
        ---
        title: [unterminated
        ---

        #publish

        Broken.
        """,
    )
    write_note(
        tmp_path / "good.md",
        """
        #publish

        Healthy post.
        """,
    )

    posts = load_posts(tmp_path)

    assert [post.slug for post in posts] == ["good"]


def test_non_mapping_frontmatter_is_skipped(tmp_path: Path) -> None:
    write_note(
        tmp_path / "list-frontmatter.md",
        """
        ---
        - one
        - two
        ---

        #publish

        Invalid frontmatter shape.
        """,
    )

    assert load_posts(tmp_path) == []


def test_post_detail_404_for_missing_or_unpublished(tmp_path: Path) -> None:
    write_note(tmp_path / "private.md", "No publish tag.")

    client = make_app(tmp_path)

    missing = client.get("/posts/missing")
    private = client.get("/posts/private")

    assert missing.status_code == 404
    assert private.status_code == 404


def test_search_matches_title_summary_and_tags(tmp_path: Path) -> None:
    write_note(
        tmp_path / "searchable-title.md",
        """
        ---
        date: 2026-03-31
        ---

        #publish

        # Searchable Title

        Alpha body.
        """,
    )
    write_note(
        tmp_path / "summary-match.md",
        """
        ---
        date: 2026-03-30
        summary: Compact beta summary
        ---

        #publish

        Body text.
        """,
    )
    write_note(
        tmp_path / "tag-match.md",
        """
        ---
        date: 2026-03-29
        tags:
          - gamma
        ---

        #publish

        Body text.
        """,
    )

    client = make_app(tmp_path)

    title_response = client.get("/posts/search", params={"q": "searchable"})
    summary_response = client.get("/posts/search", params={"q": "BETA"})
    tag_response = client.get("/posts/search", params={"q": "gamma"})

    assert [post["slug"] for post in title_response.json()] == ["searchable-title"]
    assert [post["slug"] for post in summary_response.json()] == ["summary-match"]
    assert [post["slug"] for post in tag_response.json()] == ["tag-match"]


def test_search_trims_query_whitespace(tmp_path: Path) -> None:
    write_note(
        tmp_path / "trimmed.md",
        """
        #publish

        Trim target text.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "  target  "})

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["trimmed"]


def test_search_uses_summary_fallback_and_preserves_date_order(tmp_path: Path) -> None:
    write_note(
        tmp_path / "newer.md",
        """
        ---
        date: 2026-03-31
        ---

        #publish

        Newest note mentions orbit in the first paragraph.
        """,
    )
    write_note(
        tmp_path / "older.md",
        """
        ---
        date: 2026-03-01
        ---

        #publish

        Older note also mentions orbit here.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "orbit"})

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["newer", "older"]


def test_search_returns_empty_list_for_no_matches(tmp_path: Path) -> None:
    write_note(
        tmp_path / "published.md",
        """
        #publish

        Nothing relevant here.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "missing"})

    assert response.status_code == 200
    assert response.json() == []


def test_search_rejects_missing_or_blank_queries(tmp_path: Path) -> None:
    write_note(
        tmp_path / "published.md",
        """
        #publish

        Searchable text.
        """,
    )

    client = make_app(tmp_path)

    missing = client.get("/posts/search")
    blank = client.get("/posts/search", params={"q": "   "})

    assert missing.status_code == 400
    assert blank.status_code == 400


def test_search_route_is_not_captured_by_slug_route(tmp_path: Path) -> None:
    write_note(
        tmp_path / "search.md",
        """
        #publish

        Literal slug named search.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "literal"})

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["search"]


def test_search_excludes_unpublished_notes(tmp_path: Path) -> None:
    write_note(
        tmp_path / "public.md",
        """
        #publish

        Public keyword.
        """,
    )
    write_note(
        tmp_path / "private.md",
        """
        Private keyword.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "keyword"})

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["public"]


def test_missing_vault_returns_empty_posts_and_search_results(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"
    client = make_app(missing_dir)

    posts_response = client.get("/posts")
    search_response = client.get("/posts/search", params={"q": "anything"})

    assert posts_response.status_code == 200
    assert posts_response.json() == []
    assert search_response.status_code == 200
    assert search_response.json() == []


def test_get_cached_posts_reuses_cached_result_without_vault_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_note(
        tmp_path / "published.md",
        """
        #publish

        Cached content.
        """,
    )

    call_count = 0
    original = load_posts

    def counting_load_posts(vault_dir: Path):
        nonlocal call_count
        call_count += 1
        return original(vault_dir)

    monkeypatch.setattr("main.load_posts", counting_load_posts)

    first = get_cached_posts(tmp_path)
    second = get_cached_posts(tmp_path)

    assert [post.slug for post in first] == ["published"]
    assert [post.slug for post in second] == ["published"]
    assert call_count == 1


def test_cache_invalidates_when_existing_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    note_path = tmp_path / "published.md"
    write_note(
        note_path,
        """
        #publish

        First body.
        """,
    )
    set_mtime(note_path, datetime(2026, 1, 1, 9, 0, 0))

    call_count = 0
    original = load_posts

    def counting_load_posts(vault_dir: Path):
        nonlocal call_count
        call_count += 1
        return original(vault_dir)

    monkeypatch.setattr("main.load_posts", counting_load_posts)

    first = get_cached_posts(tmp_path)
    write_note(
        note_path,
        """
        #publish

        Updated body.
        """,
    )
    set_mtime(note_path, datetime(2026, 1, 2, 9, 0, 0))
    second = get_cached_posts(tmp_path)

    assert first[0].summary == "First body."
    assert second[0].summary == "Updated body."
    assert call_count == 2


def test_cache_invalidates_when_file_is_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_note(
        tmp_path / "first.md",
        """
        #publish

        First post.
        """,
    )

    call_count = 0
    original = load_posts

    def counting_load_posts(vault_dir: Path):
        nonlocal call_count
        call_count += 1
        return original(vault_dir)

    monkeypatch.setattr("main.load_posts", counting_load_posts)

    first = get_cached_posts(tmp_path)
    write_note(
        tmp_path / "second.md",
        """
        #publish

        Second post.
        """,
    )
    second = get_cached_posts(tmp_path)

    assert [post.slug for post in first] == ["first"]
    assert [post.slug for post in second] == ["second", "first"]
    assert call_count == 2


def test_cache_invalidates_when_file_is_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_path = tmp_path / "first.md"
    second_path = tmp_path / "second.md"
    write_note(first_path, "#publish\n\nFirst post.")
    write_note(second_path, "#publish\n\nSecond post.")

    call_count = 0
    original = load_posts

    def counting_load_posts(vault_dir: Path):
        nonlocal call_count
        call_count += 1
        return original(vault_dir)

    monkeypatch.setattr("main.load_posts", counting_load_posts)

    first = get_cached_posts(tmp_path)
    second_path.unlink()
    second = get_cached_posts(tmp_path)

    assert sorted(post.slug for post in first) == ["first", "second"]
    assert [post.slug for post in second] == ["first"]
    assert call_count == 2


def test_all_read_endpoints_share_the_same_cached_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_note(
        tmp_path / "shared.md",
        """
        #publish #topic

        Shared cache body.
        """,
    )

    call_count = 0
    original = load_posts

    def counting_load_posts(vault_dir: Path):
        nonlocal call_count
        call_count += 1
        return original(vault_dir)

    monkeypatch.setattr("main.load_posts", counting_load_posts)

    client = make_app(tmp_path)

    posts_response = client.get("/posts")
    search_response = client.get("/posts/search", params={"q": "topic"})
    detail_response = client.get("/posts/shared")

    assert posts_response.status_code == 200
    assert search_response.status_code == 200
    assert detail_response.status_code == 200
    assert call_count == 1
