from __future__ import annotations

import logging
import mimetypes
import os
import re
import sqlite3
import textwrap
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import mistune
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


logger = logging.getLogger(__name__)
TAG_PATTERN = re.compile(r"(?<!\w)#([A-Za-z0-9_-]+)")
OBSIDIAN_IMAGE_PATTERN = re.compile(r"!\[\[([^\]]+)\]\]")
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
DEFAULT_CORS_ORIGINS = ["https://grenademeister.github.io"]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}


class PostSummary(BaseModel):
    title: str
    slug: str
    date: str | None
    tags: list[str]
    summary: str
    view_count: int


class Comment(BaseModel):
    id: int
    post_slug: str
    author_name: str
    body: str
    created_at: str


class CommentCreate(BaseModel):
    author_name: str
    body: str


class ViewCountResponse(BaseModel):
    slug: str
    view_count: int


class PostDetail(PostSummary):
    html: str
    comments: list[Comment]


@dataclass(frozen=True)
class Settings:
    vault_dir: Path
    cors_origins: list[str]
    db_path: Path


@dataclass(frozen=True)
class LoadedPost:
    title: str
    slug: str
    date: str | None
    sort_date: date | None
    tags: list[str]
    summary: str
    html: str


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    modified_time: float


@dataclass
class PostCache:
    fingerprint: tuple[FileFingerprint, ...] | None = None
    posts: list[LoadedPost] | None = None


@dataclass
class AssetCache:
    fingerprint: tuple[FileFingerprint, ...] | None = None
    by_name: dict[str, list[Path]] | None = None


POST_CACHE = PostCache()
ASSET_CACHE = AssetCache()


def to_post_summary(post: LoadedPost) -> PostSummary:
    return PostSummary(
        title=post.title,
        slug=post.slug,
        date=post.date,
        tags=post.tags,
        summary=post.summary,
        view_count=0,
    )


def parse_cors_origins(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def reset_post_cache() -> None:
    POST_CACHE.fingerprint = None
    POST_CACHE.posts = None


def reset_asset_cache() -> None:
    ASSET_CACHE.fingerprint = None
    ASSET_CACHE.by_name = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        vault_dir=Path(os.getenv("VAULT_DIR", "./vault_copy")).resolve(),
        cors_origins=parse_cors_origins(os.getenv("CORS_ORIGINS")),
        db_path=Path(os.getenv("DB_PATH", "./data/blog.sqlite3")).resolve(),
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_views (
                post_slug TEXT PRIMARY KEY,
                view_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_slug TEXT NOT NULL,
                author_name TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_comments_post_slug_created_at
            ON comments (post_slug, created_at)
            """
        )


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    init_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def fetch_view_counts(db_path: Path, slugs: list[str]) -> dict[str, int]:
    if not slugs:
        return {}

    placeholders = ", ".join("?" for _ in slugs)
    with get_db_connection(db_path) as connection:
        rows = connection.execute(
            f"SELECT post_slug, view_count FROM post_views WHERE post_slug IN ({placeholders})",
            slugs,
        ).fetchall()

    return {str(row["post_slug"]): int(row["view_count"]) for row in rows}


def fetch_comments(db_path: Path, slug: str) -> list[Comment]:
    with get_db_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, post_slug, author_name, body, created_at
            FROM comments
            WHERE post_slug = ?
            ORDER BY created_at ASC, id ASC
            """,
            (slug,),
        ).fetchall()

    return [Comment.model_validate(dict(row)) for row in rows]


def increment_view_count(db_path: Path, slug: str) -> int:
    now = utc_now_iso()
    with get_db_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO post_views (post_slug, view_count, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(post_slug) DO UPDATE SET
                view_count = post_views.view_count + 1,
                updated_at = excluded.updated_at
            """,
            (slug, now),
        )
        row = connection.execute(
            "SELECT view_count FROM post_views WHERE post_slug = ?",
            (slug,),
        ).fetchone()

    return 0 if row is None else int(row["view_count"])


def create_comment(db_path: Path, slug: str, payload: CommentCreate) -> Comment:
    author_name = payload.author_name.strip()
    body = payload.body.strip()
    if not author_name:
        raise HTTPException(status_code=400, detail="author_name must not be blank")
    if not body:
        raise HTTPException(status_code=400, detail="body must not be blank")

    created_at = utc_now_iso()
    with get_db_connection(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO comments (post_slug, author_name, body, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (slug, author_name, body, created_at),
        )

    return Comment(
        id=int(cursor.lastrowid),
        post_slug=slug,
        author_name=author_name,
        body=body,
        created_at=created_at,
    )


def strip_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text

    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, text

    _, remainder = parts
    frontmatter_text = parts[0][4:]
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid frontmatter: {exc}") from exc

    if not isinstance(metadata, dict):
        raise ValueError("frontmatter must be a mapping")

    return metadata, remainder


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def normalize_date(value: object) -> tuple[str | None, date | None]:
    if value is None:
        return None, None
    if isinstance(value, datetime):
        parsed = value.date()
        return parsed.isoformat(), parsed
    if isinstance(value, date):
        return value.isoformat(), value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None, None
        return parsed.isoformat(), parsed
    return None, None


def file_date(path: Path) -> tuple[str | None, date | None]:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None, None
    parsed = modified.date()
    return parsed.isoformat(), parsed


def extract_inline_tags(body: str) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for tag in TAG_PATTERN.findall(body):
        lowered = tag.lower()
        if lowered not in seen:
            seen.add(lowered)
            tags.append(lowered)
    return tags


def normalize_frontmatter_tags(value: object) -> list[str]:
    if value is None:
        return []
    raw_tags: list[str]
    if isinstance(value, str):
        raw_tags = [value]
    elif isinstance(value, list):
        raw_tags = [str(item) for item in value]
    else:
        return []

    tags: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        cleaned = tag.strip().lstrip("#").lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tags.append(cleaned)
    return tags


def extract_title(fallback: str) -> str:
    return fallback


def body_to_plain_paragraphs(body: str) -> list[str]:
    paragraphs: list[str] = []
    for chunk in re.split(r"\n\s*\n", body):
        lines = []
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# "):
                continue
            if re.fullmatch(r"(#[A-Za-z0-9_-]+\s*)+", stripped):
                continue
            lines.append(stripped)
        if not lines:
            continue
        text = " ".join(lines)
        text = re.sub(r"[*_`~>\[\]\(\)]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def strip_tag_only_lines(body: str) -> str:
    cleaned_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"(#[A-Za-z0-9_-]+\s*)+", stripped):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def extract_summary(metadata: dict, body: str, limit: int = 180) -> str:
    summary = metadata.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

    paragraphs = body_to_plain_paragraphs(body)
    if not paragraphs:
        return ""

    excerpt = paragraphs[0]
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 3].rstrip() + "..."


def combine_tags(metadata: dict, body: str) -> tuple[list[str], bool]:
    tags: list[str] = []
    seen: set[str] = set()
    has_publish = False

    for tag in [*normalize_frontmatter_tags(metadata.get("tags")), *extract_inline_tags(body)]:
        if tag == "publish":
            has_publish = True
            continue
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    return tags, has_publish


def build_markdown_renderer() -> mistune.Markdown:
    return mistune.create_markdown(hard_wrap=True, escape=False)


def media_url_for_name(asset_name: str) -> str:
    return f"/media/by-name/{quote(asset_name, safe='')}"


def media_url_for_path(asset_path: Path) -> str:
    return f"/media/by-path/{quote(asset_path.as_posix(), safe='/')}"


def resolve_relative_asset_path(note_path: Path, raw_src: str, vault_dir: Path) -> Path | None:
    cleaned = raw_src.strip().strip("<>")
    if not cleaned:
        return None
    if re.match(r"^[a-z]+:", cleaned, re.IGNORECASE) or cleaned.startswith(("/", "#", "blob:")):
        return None

    resolved = (note_path.parent / cleaned).resolve()
    vault_root = vault_dir.resolve()
    try:
        relative = resolved.relative_to(vault_root)
    except ValueError:
        return None
    if not is_image_path(relative):
        return None
    return relative


def replace_obsidian_images(body: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        parts = [part.strip() for part in inner.split("|")]
        asset_ref = parts[0]
        if not asset_ref:
            return match.group(0)

        width = next((part for part in parts[1:] if part.isdigit()), None)
        alt = Path(asset_ref).stem
        url = media_url_for_name(asset_ref)
        if width:
            return f'<img src="{url}" alt="{alt}" width="{width}" />'
        return f'<img src="{url}" alt="{alt}" />'

    return OBSIDIAN_IMAGE_PATTERN.sub(replacement, body)


def replace_markdown_local_images(body: str, note_path: Path, vault_dir: Path) -> str:
    def replacement(match: re.Match[str]) -> str:
        alt_text, raw_src = match.groups()
        cleaned_src = raw_src.strip().strip("<>")
        if re.match(r"^[a-z]+:", cleaned_src, re.IGNORECASE) or cleaned_src.startswith(("/", "#", "blob:")):
            return match.group(0)

        relative = resolve_relative_asset_path(note_path, cleaned_src, vault_dir)
        if relative is None:
            return match.group(0)
        return f"![{alt_text}]({media_url_for_path(relative)})"

    return MARKDOWN_IMAGE_PATTERN.sub(replacement, body)


def rewrite_image_links(body: str, note_path: Path, vault_dir: Path) -> str:
    body = replace_obsidian_images(body)
    return replace_markdown_local_images(body, note_path, vault_dir)


def parse_post(path: Path, renderer: mistune.Markdown, vault_dir: Path) -> LoadedPost | None:
    try:
        metadata, body = strip_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Skipping %s: %s", path, exc)
        return None

    body = textwrap.dedent(body).strip()
    tags, is_published = combine_tags(metadata, body)
    if not is_published:
        return None

    render_body = rewrite_image_links(strip_tag_only_lines(body), path, vault_dir)
    date_value, sort_date = normalize_date(metadata.get("date"))
    if date_value is None or sort_date is None:
        date_value, sort_date = file_date(path)
    slug = path.stem
    title = extract_title(slug)
    summary = extract_summary(metadata, body)
    html = renderer(render_body)

    return LoadedPost(
        title=title,
        slug=slug,
        date=date_value,
        sort_date=sort_date,
        tags=tags,
        summary=summary,
        html=html,
    )


def load_posts(vault_dir: Path) -> list[LoadedPost]:
    renderer = build_markdown_renderer()
    posts: list[LoadedPost] = []
    if not vault_dir.exists():
        logger.warning("Vault directory does not exist: %s", vault_dir)
        return posts

    for path in sorted(vault_dir.rglob("*.md")):
        post = parse_post(path, renderer, vault_dir)
        if post is not None:
            posts.append(post)

    posts.sort(key=lambda post: (post.sort_date or date.min, post.slug), reverse=True)
    return posts


def build_vault_fingerprint(vault_dir: Path) -> tuple[FileFingerprint, ...]:
    if not vault_dir.exists():
        return ()

    fingerprints: list[FileFingerprint] = []
    for path in sorted(vault_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS and path.suffix.lower() != ".md":
            continue
        try:
            modified_time = path.stat().st_mtime
        except OSError:
            continue
        fingerprints.append(FileFingerprint(path=str(path.resolve()), modified_time=modified_time))

    return tuple(fingerprints)


def get_cached_posts(vault_dir: Path) -> list[LoadedPost]:
    fingerprint = build_vault_fingerprint(vault_dir)
    if POST_CACHE.fingerprint == fingerprint and POST_CACHE.posts is not None:
        return POST_CACHE.posts

    posts = load_posts(vault_dir)
    POST_CACHE.fingerprint = fingerprint
    POST_CACHE.posts = posts
    return posts


def build_asset_fingerprint(vault_dir: Path) -> tuple[FileFingerprint, ...]:
    if not vault_dir.exists():
        return ()

    fingerprints: list[FileFingerprint] = []
    for path in sorted(vault_dir.rglob("*")):
        if not path.is_file() or not is_image_path(path):
            continue
        try:
            modified_time = path.stat().st_mtime
        except OSError:
            continue
        fingerprints.append(FileFingerprint(path=str(path.resolve()), modified_time=modified_time))
    return tuple(fingerprints)


def build_asset_index(vault_dir: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    if not vault_dir.exists():
        return {}

    for path in sorted(vault_dir.rglob("*")):
        if not path.is_file() or not is_image_path(path):
            continue
        relative = path.resolve().relative_to(vault_dir.resolve())
        index[path.name].append(relative)
    return dict(index)


def get_cached_asset_index(vault_dir: Path) -> dict[str, list[Path]]:
    fingerprint = build_asset_fingerprint(vault_dir)
    if ASSET_CACHE.fingerprint == fingerprint and ASSET_CACHE.by_name is not None:
        return ASSET_CACHE.by_name

    by_name = build_asset_index(vault_dir)
    ASSET_CACHE.fingerprint = fingerprint
    ASSET_CACHE.by_name = by_name
    return by_name


def resolve_media_path(vault_dir: Path, asset_path: Path) -> Path:
    if asset_path.is_absolute() or ".." in asset_path.parts or not is_image_path(asset_path):
        raise HTTPException(status_code=404, detail="Media not found")

    vault_root = vault_dir.resolve()
    resolved = (vault_root / asset_path).resolve()
    try:
        resolved.relative_to(vault_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Media not found")
    return resolved


def build_file_response(path: Path) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path=path, media_type=media_type, filename=path.name)


def build_search_text(post: LoadedPost) -> str:
    parts = [post.title, post.summary, *post.tags]
    return " ".join(part.lower() for part in parts if part)


def search_posts(posts: list[LoadedPost], query: str) -> list[LoadedPost]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        raise ValueError("Query must not be blank")

    if normalized_query.startswith("#"):
        tag_query = normalized_query[1:].strip()
        if not tag_query:
            raise ValueError("Tag query must include a tag name")
        return [post for post in posts if tag_query in post.tags]

    return [post for post in posts if normalized_query in build_search_text(post)]


def require_post(posts: list[LoadedPost], slug: str) -> LoadedPost:
    for post in posts:
        if post.slug == slug:
            return post
    raise HTTPException(status_code=404, detail="Post not found")


def create_app(settings: Settings | None = None, initialize_db: bool = True) -> FastAPI:
    settings = settings or get_settings()
    if initialize_db:
        init_db(settings.db_path)

    app = FastAPI(title="Obsidian Blog Backend")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"message": "Obsidian blog backend is running"}

    @app.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/posts", response_model=list[PostSummary])
    def list_posts() -> list[PostSummary]:
        posts = get_cached_posts(settings.vault_dir)
        view_counts = fetch_view_counts(settings.db_path, [post.slug for post in posts])
        return [
            PostSummary(
                title=post.title,
                slug=post.slug,
                date=post.date,
                tags=post.tags,
                summary=post.summary,
                view_count=view_counts.get(post.slug, 0),
            )
            for post in posts
        ]

    @app.get("/posts/search", response_model=list[PostSummary])
    def search_posts_endpoint(q: str | None = None) -> list[PostSummary]:
        if q is None or not q.strip():
            raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

        posts = get_cached_posts(settings.vault_dir)
        try:
            matches = search_posts(posts, q)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        view_counts = fetch_view_counts(settings.db_path, [post.slug for post in matches])
        return [
            PostSummary(
                title=post.title,
                slug=post.slug,
                date=post.date,
                tags=post.tags,
                summary=post.summary,
                view_count=view_counts.get(post.slug, 0),
            )
            for post in matches
        ]

    @app.post("/posts/{slug}/view", response_model=ViewCountResponse)
    def add_post_view(slug: str) -> ViewCountResponse:
        require_post(get_cached_posts(settings.vault_dir), slug)
        view_count = increment_view_count(settings.db_path, slug)
        return ViewCountResponse(slug=slug, view_count=view_count)

    @app.post("/posts/{slug}/comments", response_model=Comment)
    def add_post_comment(slug: str, payload: CommentCreate) -> Comment:
        require_post(get_cached_posts(settings.vault_dir), slug)
        return create_comment(settings.db_path, slug, payload)

    @app.get("/media/by-name/{asset_name}")
    def get_media_by_name(asset_name: str) -> FileResponse:
        if "/" in asset_name or "\\" in asset_name:
            raise HTTPException(status_code=404, detail="Media not found")
        if not is_image_path(Path(asset_name)):
            raise HTTPException(status_code=404, detail="Media not found")

        asset_index = get_cached_asset_index(settings.vault_dir)
        matches = asset_index.get(asset_name)
        if not matches:
            raise HTTPException(status_code=404, detail="Media not found")
        path = resolve_media_path(settings.vault_dir, matches[0])
        return build_file_response(path)

    @app.get("/media/by-path/{asset_path:path}")
    def get_media_by_path(asset_path: str) -> FileResponse:
        path = resolve_media_path(settings.vault_dir, Path(asset_path))
        return build_file_response(path)

    @app.get("/posts/{slug}", response_model=PostDetail)
    def get_post(slug: str) -> PostDetail:
        post = require_post(get_cached_posts(settings.vault_dir), slug)
        return PostDetail(
            title=post.title,
            slug=post.slug,
            date=post.date,
            tags=post.tags,
            summary=post.summary,
            view_count=fetch_view_counts(settings.db_path, [slug]).get(slug, 0),
            html=post.html,
            comments=fetch_comments(settings.db_path, slug),
        )

    return app


app = create_app(initialize_db=False)


def main() -> None:
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
