from __future__ import annotations

import logging
import os
import re
import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import mistune
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


logger = logging.getLogger(__name__)
TAG_PATTERN = re.compile(r"(?<!\w)#([A-Za-z0-9_-]+)")
DEFAULT_CORS_ORIGINS = ["https://grenademeister.github.io"]


class PostSummary(BaseModel):
    title: str
    slug: str
    date: str | None
    tags: list[str]
    summary: str


class PostDetail(PostSummary):
    html: str


@dataclass(frozen=True)
class Settings:
    vault_dir: Path
    cors_origins: list[str]


@dataclass(frozen=True)
class LoadedPost:
    title: str
    slug: str
    date: str | None
    sort_date: date | None
    tags: list[str]
    summary: str
    html: str


def to_post_summary(post: LoadedPost) -> PostSummary:
    return PostSummary(
        title=post.title,
        slug=post.slug,
        date=post.date,
        tags=post.tags,
        summary=post.summary,
    )


def parse_cors_origins(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        vault_dir=Path(os.getenv("VAULT_DIR", "./stub_vault")).resolve(),
        cors_origins=parse_cors_origins(os.getenv("CORS_ORIGINS")),
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
    return mistune.create_markdown()


def parse_post(path: Path, renderer: mistune.Markdown) -> LoadedPost | None:
    try:
        metadata, body = strip_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Skipping %s: %s", path, exc)
        return None

    body = textwrap.dedent(body).strip()
    tags, is_published = combine_tags(metadata, body)
    if not is_published:
        return None

    render_body = strip_tag_only_lines(body)
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
        post = parse_post(path, renderer)
        if post is not None:
            posts.append(post)

    posts.sort(key=lambda post: (post.sort_date or date.min, post.slug), reverse=True)
    return posts


def build_search_text(post: LoadedPost) -> str:
    parts = [post.title, post.summary, *post.tags]
    return " ".join(part.lower() for part in parts if part)


def search_posts(posts: list[LoadedPost], query: str) -> list[LoadedPost]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        raise ValueError("Query must not be blank")

    return [post for post in posts if normalized_query in build_search_text(post)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(title="Obsidian Blog Backend")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
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
        posts = load_posts(settings.vault_dir)
        return [to_post_summary(post) for post in posts]

    @app.get("/posts/search", response_model=list[PostSummary])
    def search_posts_endpoint(q: str | None = None) -> list[PostSummary]:
        if q is None or not q.strip():
            raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

        posts = load_posts(settings.vault_dir)
        return [to_post_summary(post) for post in search_posts(posts, q)]

    @app.get("/posts/{slug}", response_model=PostDetail)
    def get_post(slug: str) -> PostDetail:
        for post in load_posts(settings.vault_dir):
            if post.slug == slug:
                return PostDetail(
                    title=post.title,
                    slug=post.slug,
                    date=post.date,
                    tags=post.tags,
                    summary=post.summary,
                    html=post.html,
                )
        raise HTTPException(status_code=404, detail="Post not found")

    return app


app = create_app()


def main() -> None:
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
