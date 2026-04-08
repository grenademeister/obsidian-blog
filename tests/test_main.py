import hashlib
import sys
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

import os
import sqlite3
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from main import Settings, create_app, get_cached_posts, load_posts, reset_asset_cache, reset_post_cache


def make_db_path(vault_dir: Path) -> Path:
    digest = hashlib.sha1(str(vault_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"obsidian-blog-test-{digest}.sqlite3"


def make_app(tmp_path: Path) -> TestClient:
    app = create_app(
        Settings(
            vault_dir=tmp_path,
            cors_origins=["https://grenademeister.github.io"],
            db_path=make_db_path(tmp_path),
        )
    )
    return TestClient(app)


def write_note(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def set_mtime(path: Path, timestamp: datetime) -> None:
    unix_time = timestamp.timestamp()
    os.utime(path, (unix_time, unix_time))


def write_binary(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture(autouse=True)
def clear_post_cache() -> None:
    reset_post_cache()
    reset_asset_cache()
    yield
    reset_post_cache()
    reset_asset_cache()


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
    assert "nested/deep-note" in slugs


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
    assert response.json()[0]["view_count"] == 0


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
    assert payload["view_count"] == 0
    assert payload["comments"] == []


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


def test_post_detail_preserves_single_line_breaks_as_br(tmp_path: Path) -> None:
    write_note(
        tmp_path / "line-breaks.md",
        """
        #publish

        first line
        second line
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/line-breaks")

    assert response.status_code == 200
    assert "first line<br />" in response.json()["html"]


def test_post_detail_escapes_raw_html_and_scripts(tmp_path: Path) -> None:
    write_note(
        tmp_path / "unsafe-html.md",
        """
        #publish

        <script>alert("xss")</script>
        <img src="https://example.com/x.png" onerror="alert('xss')" />
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/unsafe-html")

    assert response.status_code == 200
    html = response.json()["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;alert" in html
    assert "<img " not in html
    assert " onerror=" in html
    assert "&lt;img src=" in html
    assert "&lt;/script&gt;" in html


def test_post_detail_escapes_raw_html_attributes_and_inline_tags(tmp_path: Path) -> None:
    write_note(
        tmp_path / "unsafe-attrs.md",
        """
        #publish

        <div onclick="alert('xss')">raw html</div>

        Prefix <span data-unsafe="yes">inline html</span> suffix.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/unsafe-attrs")

    assert response.status_code == 200
    html = response.json()["html"]
    assert "<div" not in html
    assert "<span" not in html
    assert "&lt;div onclick=" in html
    assert "&lt;span data-unsafe=" in html
    assert "Prefix &lt;span data-unsafe=&quot;yes&quot;&gt;inline html&lt;/span&gt; suffix." in html


def test_post_detail_rejects_unsafe_link_schemes(tmp_path: Path) -> None:
    write_note(
        tmp_path / "unsafe-links.md",
        """
        #publish

        [bad](javascript:alert('xss'))
        [mail](mailto:test@example.com)
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/unsafe-links")

    assert response.status_code == 200
    html = response.json()["html"]
    assert 'href="javascript:alert' not in html
    assert ">bad</a>" not in html
    assert 'href="mailto:test@example.com"' in html


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


def test_obsidian_image_embed_renders_media_url(tmp_path: Path) -> None:
    write_binary(tmp_path / "00_Meta" / "sample.jpg", b"fake-jpg")
    write_note(
        tmp_path / "image-note.md",
        """
        #publish

        ![[sample.jpg]]
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/image-note")

    assert response.status_code == 200
    assert '<img src="/media/by-name/sample.jpg" alt="sample" />' in response.json()["html"]


def test_obsidian_image_embed_with_width_renders_img_tag_width(tmp_path: Path) -> None:
    write_binary(tmp_path / "00_Meta" / "wide.png", b"fake-png")
    write_note(
        tmp_path / "wide-image.md",
        """
        #publish

        ![[wide.png|200]]
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/wide-image")

    assert response.status_code == 200
    assert 'src="/media/by-name/wide.png"' in response.json()["html"]
    assert 'width="200"' in response.json()["html"]


def test_relative_markdown_image_renders_media_by_path_url(tmp_path: Path) -> None:
    write_binary(tmp_path / "00_Meta" / "growth.png", b"fake-growth")
    write_note(
        tmp_path / "nested" / "relative-image.md",
        """
        #publish

        ![Progressive Overload](../00_Meta/growth.png)
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/nested/relative-image")

    assert response.status_code == 200
    assert "/media/by-path/00_Meta/growth.png" in response.json()["html"]


def test_remote_markdown_image_url_is_left_unchanged(tmp_path: Path) -> None:
    write_note(
        tmp_path / "remote-image.md",
        """
        #publish

        ![Remote](https://example.com/image.png)
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/remote-image")

    assert response.status_code == 200
    assert 'src="https://example.com/image.png"' in response.json()["html"]


def test_post_detail_renders_code_tables_blockquotes_and_external_links(tmp_path: Path) -> None:
    write_note(
        tmp_path / "technical-writing.md",
        """
        #publish

        Here is `inline code`.

        ```python
        print("hello")
        ```

        > Quoted note.

        | Col A | Col B |
        | --- | --- |
        | 1 | 2 |

        [Docs](https://example.com/docs)
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/technical-writing")

    assert response.status_code == 200
    html = response.json()["html"]
    assert '<code class="inline-code">inline code</code>' in html
    assert '<pre class="code-block"><code class="language-python">' in html
    assert "print(&quot;hello&quot;)" in html
    assert "</code></pre>" in html
    assert '<blockquote class="prose-quote">' in html
    assert '<div class="table-scroll"><table>' in html
    assert "<thead>" in html
    assert "<tbody>" in html
    assert "<th>Col A</th>" in html
    assert "<td>2</td>" in html
    assert 'href="https://example.com/docs"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer nofollow"' in html


def test_media_by_name_serves_image_bytes(tmp_path: Path) -> None:
    image_bytes = b"fake-image-data"
    write_binary(tmp_path / "00_Meta" / "asset.jpg", image_bytes)

    client = make_app(tmp_path)
    response = client.get("/media/by-name/asset.jpg")

    assert response.status_code == 200
    assert response.content == image_bytes
    assert response.headers["content-type"].startswith("image/jpeg")


def test_media_by_path_serves_image_bytes(tmp_path: Path) -> None:
    image_bytes = b"fake-png-data"
    write_binary(tmp_path / "images" / "chart.png", image_bytes)

    client = make_app(tmp_path)
    response = client.get("/media/by-path/images/chart.png")

    assert response.status_code == 200
    assert response.content == image_bytes
    assert response.headers["content-type"].startswith("image/png")


def test_media_endpoints_reject_traversal_and_non_image_files(tmp_path: Path) -> None:
    write_note(
        tmp_path / "plain.md",
        """
        #publish

        Body.
        """,
    )

    client = make_app(tmp_path)
    traversal = client.get("/media/by-path/../plain.md")
    non_image = client.get("/media/by-name/plain.md")

    assert traversal.status_code == 404
    assert non_image.status_code == 404


def test_loader_reads_nested_markdown_files(tmp_path: Path) -> None:
    write_note(
        tmp_path / "nested" / "path" / "deep-post.md",
        """
        #publish

        Deep content.
        """,
    )

    posts = load_posts(tmp_path)

    assert [post.slug for post in posts] == ["nested/path/deep-post"]


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


def test_duplicate_basenames_use_unique_vault_relative_slugs(tmp_path: Path) -> None:
    write_note(
        tmp_path / "alpha" / "shared.md",
        """
        ---
        date: 2026-03-31
        ---

        #publish #common

        Alpha content.
        """,
    )
    write_note(
        tmp_path / "beta team" / "shared.md",
        """
        ---
        date: 2026-03-30
        ---

        #publish #common

        Beta content.
        """,
    )

    client = make_app(tmp_path)

    posts_response = client.get("/posts")
    alpha_detail = client.get("/posts/alpha/shared")
    beta_detail = client.get("/posts/beta%20team/shared")
    search_response = client.get("/posts/search", params={"q": "common"})
    first_view = client.post("/posts/alpha/shared/view")
    second_view = client.post("/posts/beta%20team/shared/view")
    first_comment = client.post(
        "/posts/alpha/shared/comments",
        json={"author_name": "Alice", "body": "Alpha comment."},
    )
    second_comment = client.post(
        "/posts/beta%20team/shared/comments",
        json={"author_name": "Bob", "body": "Beta comment."},
    )

    assert posts_response.status_code == 200
    assert [post["slug"] for post in posts_response.json()] == ["alpha/shared", "beta team/shared"]
    assert alpha_detail.status_code == 200
    assert alpha_detail.json()["summary"] == "Alpha content."
    assert beta_detail.status_code == 200
    assert beta_detail.json()["summary"] == "Beta content."
    assert [post["slug"] for post in search_response.json()] == ["alpha/shared", "beta team/shared"]
    assert first_view.json() == {"slug": "alpha/shared", "view_count": 1}
    assert second_view.json() == {"slug": "beta team/shared", "view_count": 1}
    assert first_comment.json()["post_slug"] == "alpha/shared"
    assert second_comment.json()["post_slug"] == "beta team/shared"
    assert [comment["body"] for comment in client.get("/posts/alpha/shared").json()["comments"]] == ["Alpha comment."]
    assert [comment["body"] for comment in client.get("/posts/beta%20team/shared").json()["comments"]] == ["Beta comment."]


def test_path_like_slug_routes_support_spaces_and_slashes(tmp_path: Path) -> None:
    write_note(
        tmp_path / "nested folder" / "space note.md",
        """
        #publish

        Route content.
        """,
    )

    client = make_app(tmp_path)

    detail = client.get("/posts/nested%20folder/space%20note")
    first_view = client.post("/posts/nested%20folder/space%20note/view")
    comment = client.post(
        "/posts/nested%20folder/space%20note/comments",
        json={"author_name": "Casey", "body": "Works."},
    )

    assert detail.status_code == 200
    assert detail.json()["slug"] == "nested folder/space note"
    assert first_view.status_code == 200
    assert first_view.json() == {"slug": "nested folder/space note", "view_count": 1}
    assert comment.status_code == 200
    assert comment.json()["post_slug"] == "nested folder/space note"


def test_startup_initializes_sqlite_schema(tmp_path: Path) -> None:
    db_path = make_db_path(tmp_path)
    client = make_app(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert db_path.exists()

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "post_views" in tables
    assert "comments" in tables


def test_post_list_and_detail_include_default_db_fields(tmp_path: Path) -> None:
    write_note(
        tmp_path / "post.md",
        """
        #publish

        Body.
        """,
    )

    client = make_app(tmp_path)

    posts_response = client.get("/posts")
    detail_response = client.get("/posts/post")

    assert posts_response.status_code == 200
    assert posts_response.json()[0]["view_count"] == 0
    assert detail_response.status_code == 200
    assert detail_response.json()["view_count"] == 0
    assert detail_response.json()["comments"] == []


def test_post_view_endpoint_increments_persistently(tmp_path: Path) -> None:
    write_note(
        tmp_path / "post.md",
        """
        #publish

        Body.
        """,
    )

    client = make_app(tmp_path)

    first = client.post("/posts/post/view")
    second = client.post("/posts/post/view")
    detail = client.get("/posts/post")
    posts = client.get("/posts")

    assert first.status_code == 200
    assert first.json() == {"slug": "post", "view_count": 1}
    assert second.status_code == 200
    assert second.json() == {"slug": "post", "view_count": 2}
    assert detail.json()["view_count"] == 2
    assert posts.json()[0]["view_count"] == 2


def test_post_view_endpoint_rejects_missing_or_unpublished_posts(tmp_path: Path) -> None:
    write_note(tmp_path / "private.md", "Private.")
    client = make_app(tmp_path)

    missing = client.post("/posts/missing/view")
    private = client.post("/posts/private/view")

    assert missing.status_code == 404
    assert private.status_code == 404


def test_post_comment_creation_and_ordering(tmp_path: Path) -> None:
    write_note(
        tmp_path / "post.md",
        """
        #publish

        Body.
        """,
    )

    client = make_app(tmp_path)

    first = client.post(
        "/posts/post/comments",
        json={"author_name": " Alice ", "body": " First comment. "},
    )
    second = client.post(
        "/posts/post/comments",
        json={"author_name": "Bob", "body": "Second comment."},
    )
    detail = client.get("/posts/post")

    assert first.status_code == 200
    assert first.json()["author_name"] == "Alice"
    assert first.json()["body"] == "First comment."
    assert second.status_code == 200
    assert [comment["author_name"] for comment in detail.json()["comments"]] == ["Alice", "Bob"]
    assert [comment["body"] for comment in detail.json()["comments"]] == ["First comment.", "Second comment."]


def test_post_comment_creation_rejects_blank_fields(tmp_path: Path) -> None:
    write_note(
        tmp_path / "post.md",
        """
        #publish

        Body.
        """,
    )

    client = make_app(tmp_path)

    blank_author = client.post(
        "/posts/post/comments",
        json={"author_name": "   ", "body": "Valid"},
    )
    blank_body = client.post(
        "/posts/post/comments",
        json={"author_name": "Valid", "body": "   "},
    )

    assert blank_author.status_code == 400
    assert blank_body.status_code == 400


def test_post_comment_creation_rejects_missing_or_unpublished_posts(tmp_path: Path) -> None:
    write_note(tmp_path / "private.md", "Private.")
    client = make_app(tmp_path)

    missing = client.post(
        "/posts/missing/comments",
        json={"author_name": "A", "body": "Hello"},
    )
    private = client.post(
        "/posts/private/comments",
        json={"author_name": "A", "body": "Hello"},
    )

    assert missing.status_code == 404
    assert private.status_code == 404


def test_db_data_persists_across_app_instances(tmp_path: Path) -> None:
    write_note(
        tmp_path / "post.md",
        """
        #publish

        Body.
        """,
    )

    first_client = make_app(tmp_path)
    first_client.post("/posts/post/view")
    first_client.post(
        "/posts/post/comments",
        json={"author_name": "Alice", "body": "Persistent comment."},
    )

    second_client = make_app(tmp_path)
    detail = second_client.get("/posts/post")

    assert detail.status_code == 200
    assert detail.json()["view_count"] == 1
    assert [comment["body"] for comment in detail.json()["comments"]] == ["Persistent comment."]


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


def test_hash_prefixed_search_matches_exact_tag_name(tmp_path: Path) -> None:
    write_note(
        tmp_path / "todo-note.md",
        """
        ---
        tags:
          - todo
        ---

        #publish

        Task note.
        """,
    )
    write_note(
        tmp_path / "todo-list.md",
        """
        ---
        tags:
          - todo-list
        ---

        #publish

        Similar but different tag.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "#todo"})

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["todo-note"]


def test_hash_prefixed_search_is_case_insensitive(tmp_path: Path) -> None:
    write_note(
        tmp_path / "mixed-tag.md",
        """
        ---
        tags:
          - ToDo
        ---

        #publish

        Mixed case tag.
        """,
    )

    client = make_app(tmp_path)
    response = client.get("/posts/search", params={"q": "#TODO"})

    assert response.status_code == 200
    assert [post["slug"] for post in response.json()] == ["mixed-tag"]


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
    tag_only = client.get("/posts/search", params={"q": "#"})

    assert missing.status_code == 400
    assert blank.status_code == 400
    assert tag_only.status_code == 400


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
