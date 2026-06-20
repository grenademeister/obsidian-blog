from __future__ import annotations

import hashlib
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
from html import escape
from io import BytesIO
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse

import mistune
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from mistune.plugins.math import math_in_list, math_in_quote
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel


logger = logging.getLogger(__name__)
TAG_PATTERN = re.compile(r"(?<!\w)#([A-Za-z0-9_-]+)")
OBSIDIAN_IMAGE_PATTERN = re.compile(r"!\[\[([^\]]+)\]\]")
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
DEFAULT_CORS_ORIGINS = ["https://grenademeister.github.io"]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
SAFE_LINK_SCHEMES = {"http", "https", "mailto"}
SAFE_IMAGE_SCHEMES = {"http", "https"}
THUMBNAIL_MAX_SIZE = (480, 480)
THUMBNAIL_WEBP_QUALITY = 70
MEDIA_WEBP_QUALITY = 85
MEDIA_MAX_SIZE = (1920, 1920)
MEDIA_CACHE_MAX_AGE = 31536000


class PostSummary(BaseModel):
    title: str
    slug: str
    date: str | None
    tags: list[str]
    summary: str
    view_count: int
    thumbnail_id: str | None


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
    media_cache_dir: Path


@dataclass(frozen=True)
class LoadedPost:
    title: str
    slug: str
    date: str | None
    sort_date: date | None
    tags: list[str]
    summary: str
    thumbnail_id: str | None
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
        thumbnail_id=post.thumbnail_id,
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
        media_cache_dir=Path(os.getenv("MEDIA_CACHE_DIR", "./data/media_cache")).resolve(),
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
        migrate_path_view_counts(connection)


def migrate_path_view_counts(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT post_slug, view_count, updated_at
        FROM post_views
        WHERE instr(post_slug, '/') > 0
        """
    ).fetchall()
    for post_slug, view_count, updated_at in rows:
        filename_key = view_count_key(str(post_slug))
        connection.execute(
            """
            INSERT INTO post_views (post_slug, view_count, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(post_slug) DO UPDATE SET
                view_count = post_views.view_count + excluded.view_count,
                updated_at = MAX(post_views.updated_at, excluded.updated_at)
            """,
            (filename_key, view_count, updated_at),
        )
        connection.execute("DELETE FROM post_views WHERE post_slug = ?", (post_slug,))


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
    if not fallback:
        return fallback
    return PurePosixPath(fallback).name


def slug_for_path(path: Path, vault_dir: Path) -> str:
    return path.relative_to(vault_dir).with_suffix("").as_posix()


def view_count_key(slug: str) -> str:
    return PurePosixPath(slug).name


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
    renderer = BlogRenderer()
    markdown = mistune.create_markdown(
        renderer=renderer,
        hard_wrap=True,
        escape=True,
        plugins=["table", "url", "strikethrough", "math", math_in_quote, math_in_list],
    )
    renderer.register("inline_math", render_inline_math)
    renderer.register("block_math", render_block_math)
    return markdown


def render_inline_math(renderer: mistune.BaseRenderer, text: str) -> str:
    return rf'<span class="math">\({escape(text)}\)</span>'


def render_block_math(renderer: mistune.BaseRenderer, text: str) -> str:
    return f'<div class="math">$$\n{escape(text)}\n$$</div>\n'


def is_safe_url(url: str, *, allowed_schemes: set[str], allow_relative: bool) -> bool:
    candidate = url.strip()
    if not candidate:
        return False
    if candidate.startswith(("data:", "javascript:", "vbscript:")):
        return False

    parsed = urlparse(candidate)
    if parsed.scheme:
        return parsed.scheme.lower() in allowed_schemes

    if not allow_relative:
        return False

    return candidate.startswith(("/", "./", "../", "#"))


def image_markdown(alt: str, url: str, title: str | None = None) -> str:
    escaped_alt = alt.replace("[", r"\[").replace("]", r"\]")
    escaped_url = url.replace(" ", "%20")
    if title:
        escaped_title = title.replace('"', '\\"')
        return f'![{escaped_alt}]({escaped_url} "{escaped_title}")'
    return f"![{escaped_alt}]({escaped_url})"


class BlogRenderer(mistune.HTMLRenderer):
    def inline_html(self, html: str) -> str:
        return escape(html)

    def block_html(self, html: str) -> str:
        return f"{escape(html)}\n"

    def link(self, text: str, url: str, title: str | None = None) -> str:
        if not is_safe_url(url, allowed_schemes=SAFE_LINK_SCHEMES, allow_relative=True):
            return escape(text or url)

        attrs = [f'href="{escape(url, quote=True)}"']
        if title:
            attrs.append(f'title="{escape(title, quote=True)}"')

        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            attrs.append('target="_blank"')
            attrs.append('rel="noopener noreferrer nofollow"')

        return f"<a {' '.join(attrs)}>{text}</a>"

    def image(self, text: str, url: str, title: str | None = None) -> str:
        if not is_safe_url(url, allowed_schemes=SAFE_IMAGE_SCHEMES, allow_relative=True):
            return escape(text or "")

        attrs = [
            f'src="{escape(url, quote=True)}"',
            f'alt="{escape(text or "", quote=True)}"',
        ]
        if title:
            width_match = re.fullmatch(r"width=(\d+)", title.strip())
            if width_match:
                attrs.append(f'width="{width_match.group(1)}"')
            else:
                attrs.append(f'title="{escape(title, quote=True)}"')

        return f"<img {' '.join(attrs)} />"

    def block_code(self, code: str, info: str | None = None) -> str:
        language = ""
        if info:
            language = info.strip().split(None, 1)[0]

        code_class = f' class="language-{escape(language, quote=True)}"' if language else ""
        pre_class = " class=\"code-block\""
        return f"<pre{pre_class}><code{code_class}>{escape(code)}</code></pre>\n"

    def codespan(self, text: str) -> str:
        return f'<code class="inline-code">{escape(text)}</code>'

    def block_quote(self, text: str) -> str:
        return f'<blockquote class="prose-quote">{text}</blockquote>\n'

    def table(self, text: str) -> str:
        return f'<div class="table-scroll"><table>{text}</table></div>\n'


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


def clean_local_image_ref(raw_ref: object) -> str | None:
    if not isinstance(raw_ref, str):
        return None

    cleaned = raw_ref.strip().strip("<>")
    if not cleaned:
        return None
    cleaned = cleaned.split("|", 1)[0].strip()
    cleaned = cleaned.split("#", 1)[0].strip()
    if not cleaned:
        return None
    if re.match(r"^[a-z]+:", cleaned, re.IGNORECASE) or cleaned.startswith(("/", "#", "blob:")):
        return None
    return cleaned


def resolve_asset_reference(note_path: Path, raw_ref: object, vault_dir: Path) -> Path | None:
    cleaned = clean_local_image_ref(raw_ref)
    if cleaned is None:
        return None

    candidate = Path(cleaned)
    if candidate.is_absolute() or not is_image_path(candidate):
        return None

    vault_root = vault_dir.resolve()
    if ".." not in candidate.parts and len(candidate.parts) > 1:
        resolved = (vault_root / candidate).resolve()
        try:
            relative = resolved.relative_to(vault_root)
        except ValueError:
            return None
        if resolved.is_file():
            return relative

    if ".." not in candidate.parts and len(candidate.parts) == 1:
        matches = build_asset_index(vault_dir).get(candidate.name, [])
        if matches:
            return matches[0]

    relative = resolve_relative_asset_path(note_path, cleaned, vault_dir)
    if relative is None:
        return None
    if (vault_root / relative).is_file():
        return relative
    return None


def first_local_body_image(note_path: Path, body: str, vault_dir: Path) -> Path | None:
    refs: list[tuple[int, str]] = []
    refs.extend((match.start(), match.group(1)) for match in OBSIDIAN_IMAGE_PATTERN.finditer(body))
    refs.extend((match.start(), match.group(2)) for match in MARKDOWN_IMAGE_PATTERN.finditer(body))

    for _, raw_ref in sorted(refs, key=lambda item: item[0]):
        relative = resolve_asset_reference(note_path, raw_ref, vault_dir)
        if relative is not None:
            return relative

    return None


def thumbnail_id_for_post(metadata: dict, body: str, note_path: Path, vault_dir: Path) -> str | None:
    frontmatter_thumbnail = resolve_asset_reference(note_path, metadata.get("thumbnail"), vault_dir)
    if frontmatter_thumbnail is not None:
        return frontmatter_thumbnail.as_posix()

    body_thumbnail = first_local_body_image(note_path, body, vault_dir)
    if body_thumbnail is not None:
        return body_thumbnail.as_posix()
    return None


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
        title = f"width={width}" if width else None
        return image_markdown(alt, url, title)

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
    slug = slug_for_path(path, vault_dir)
    title = extract_title(slug)
    summary = extract_summary(metadata, body)
    thumbnail_id = thumbnail_id_for_post(metadata, body, path, vault_dir)
    html = renderer(render_body)

    return LoadedPost(
        title=title,
        slug=slug,
        date=date_value,
        sort_date=sort_date,
        tags=tags,
        summary=summary,
        thumbnail_id=thumbnail_id,
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


def _media_cache_key(relative_path: Path) -> str:
    return hashlib.sha256(relative_path.as_posix().encode()).hexdigest()[:16]


def build_file_response(path: Path, cache_dir: Path, if_none_match: str | None = None) -> Response:
    ext = path.suffix.lower()

    try:
        source_mtime = int(path.stat().st_mtime)
    except OSError:
        source_mtime = 0

    etag = f'"{source_mtime}"'

    if if_none_match == etag:
        return Response(status_code=304)

    if ext in {".svg", ".gif"}:
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path=path, media_type=media_type, filename=path.name)

    cache_file = None
    cache_key = None
    try:
        source_path = path.resolve()
        cache_key = _media_cache_key(source_path)
        cache_file = cache_dir / f"{cache_key}_{source_mtime}.webp"
        if cache_file.is_file():
            return Response(
                content=cache_file.read_bytes(),
                media_type="image/webp",
                headers={
                    "Cache-Control": f"public, max-age={MEDIA_CACHE_MAX_AGE}, immutable",
                    "ETag": etag,
                    "Content-Disposition": f'inline; filename="{path.stem}.webp"',
                },
            )
    except (ValueError, OSError):
        pass

    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)

            if image.mode not in {"RGB", "RGBA"}:
                has_transparency = "transparency" in image.info
                image = image.convert("RGBA" if has_transparency else "RGB")

            image.thumbnail(MEDIA_MAX_SIZE, Image.Resampling.LANCZOS)

            buffer = BytesIO()
            image.save(buffer, format="WEBP", quality=MEDIA_WEBP_QUALITY, method=6)
            content = buffer.getvalue()
    except (OSError, UnidentifiedImageError):
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path=path, media_type=media_type, filename=path.name)

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        for old in cache_file.parent.glob(f"{cache_key}_*.webp"):
            if old.name != cache_file.name:
                old.unlink(missing_ok=True)
        cache_file.write_bytes(content)

    return Response(
        content=content,
        media_type="image/webp",
        headers={
            "Cache-Control": f"public, max-age={MEDIA_CACHE_MAX_AGE}, immutable",
            "ETag": etag,
            "Content-Disposition": f'inline; filename="{path.stem}.webp"',
        },
    )


def build_thumbnail_response(path: Path) -> Response:
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)

            if image.mode not in {"RGB", "RGBA"}:
                has_transparency = "transparency" in image.info
                image = image.convert("RGBA" if has_transparency else "RGB")

            buffer = BytesIO()
            image.save(buffer, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY, method=6)
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(status_code=404, detail="Thumbnail not found") from exc

    return Response(
        content=buffer.getvalue(),
        media_type="image/webp",
        headers={
            "Content-Disposition": f'inline; filename="{path.stem}.webp"',
        },
    )


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
        view_counts = fetch_view_counts(settings.db_path, [view_count_key(post.slug) for post in posts])
        return [
            PostSummary(
                title=post.title,
                slug=post.slug,
                date=post.date,
                tags=post.tags,
                summary=post.summary,
                view_count=view_counts.get(view_count_key(post.slug), 0),
                thumbnail_id=post.thumbnail_id,
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
        view_counts = fetch_view_counts(settings.db_path, [view_count_key(post.slug) for post in matches])
        return [
            PostSummary(
                title=post.title,
                slug=post.slug,
                date=post.date,
                tags=post.tags,
                summary=post.summary,
                view_count=view_counts.get(view_count_key(post.slug), 0),
                thumbnail_id=post.thumbnail_id,
            )
            for post in matches
        ]

    @app.post("/posts/{slug:path}/view", response_model=ViewCountResponse)
    def add_post_view(slug: str) -> ViewCountResponse:
        post = require_post(get_cached_posts(settings.vault_dir), slug)
        view_count = increment_view_count(settings.db_path, view_count_key(post.slug))
        return ViewCountResponse(slug=slug, view_count=view_count)

    @app.post("/posts/{slug:path}/comments", response_model=Comment)
    def add_post_comment(slug: str, payload: CommentCreate) -> Comment:
        require_post(get_cached_posts(settings.vault_dir), slug)
        return create_comment(settings.db_path, slug, payload)

    @app.get("/media/by-name/{asset_name}")
    def get_media_by_name(asset_name: str, request: Request) -> Response:
        if "/" in asset_name or "\\" in asset_name:
            raise HTTPException(status_code=404, detail="Media not found")
        if not is_image_path(Path(asset_name)):
            raise HTTPException(status_code=404, detail="Media not found")

        asset_index = get_cached_asset_index(settings.vault_dir)
        matches = asset_index.get(asset_name)
        if not matches:
            raise HTTPException(status_code=404, detail="Media not found")
        path = resolve_media_path(settings.vault_dir, matches[0])
        return build_file_response(path, settings.media_cache_dir, request.headers.get("if-none-match"))

    @app.get("/media/by-path/{asset_path:path}")
    def get_media_by_path(asset_path: str, request: Request) -> Response:
        path = resolve_media_path(settings.vault_dir, Path(asset_path))
        return build_file_response(path, settings.media_cache_dir, request.headers.get("if-none-match"))

    @app.get("/thumbnail/{thumbnail_id:path}")
    def get_thumbnail(thumbnail_id: str) -> Response:
        path = resolve_media_path(settings.vault_dir, Path(thumbnail_id))
        return build_thumbnail_response(path)

    @app.get("/posts/{slug:path}", response_model=PostDetail)
    def get_post(slug: str) -> PostDetail:
        post = require_post(get_cached_posts(settings.vault_dir), slug)
        stored_view_count = fetch_view_counts(settings.db_path, [view_count_key(post.slug)]).get(
            view_count_key(post.slug), 0
        )
        return PostDetail(
            title=post.title,
            slug=post.slug,
            date=post.date,
            tags=post.tags,
            summary=post.summary,
            view_count=stored_view_count,
            thumbnail_id=post.thumbnail_id,
            html=post.html,
            comments=fetch_comments(settings.db_path, slug),
        )

    return app


app = create_app(initialize_db=False)


def main() -> None:
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
