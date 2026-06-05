## Obsidian Blog FastAPI Backend

Small FastAPI backend for publishing selected Obsidian notes as a read-only blog API.

The service reads Markdown files from a vault directory on disk, filters to public notes, converts Markdown to HTML, and exposes simple JSON endpoints for a static frontend.

### What It Does

- Reads `*.md` files from a configured vault directory.
- Parses optional YAML frontmatter.
- Publishes only notes containing `#publish`.
- Returns post summaries through `/posts`.
- Returns rendered post content through `/posts/{slug}`.
- Serves local note images through `/media/...` and post thumbnails through `/thumbnail/...`.
- Provides simple search through `/posts/search?q=...`.
- Uses direct disk reads when the vault changes, with an in-process cache between changes.
- Stores view counts and comments in a small local SQLite database.

### Quick Start

Install dependencies:

```bash
uv sync
```

Run the server:

```bash
uv run main.py
```

Default local URLs:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/posts`
- `http://127.0.0.1:8000/posts/search?q=frontmatter`
- `http://127.0.0.1:8000/posts/with-frontmatter`

Run tests:

```bash
uv run pytest -q
```

### Configuration

The backend is configured with environment variables.

```bash
export VAULT_DIR=./vault_copy
export CORS_ORIGINS=https://grenademeister.github.io
export DB_PATH=./data/blog.sqlite3
```

Available settings:

- `VAULT_DIR`
  - Path to the Markdown vault to read from.
  - Default: `./vault_copy`
  - The default is a local copy of the real vault so development does not read directly from the synced source.
- `CORS_ORIGINS`
  - Comma-separated list of allowed origins.
  - Default: `https://grenademeister.github.io`
  - Example: `http://localhost:5173,https://grenademeister.github.io`
- `DB_PATH`
  - Path to the SQLite database file used for view counts and comments.
  - Default: `./data/blog.sqlite3`

### Content Model

Notes are standard Markdown files with optional YAML frontmatter.

Example:

```md
---
title: My Post
date: 2026-03-30
tags:
  - ai
  - notes
summary: Short preview text.
thumbnail: 00_Meta/cover.jpg
---

#publish #research

# My Post

Hello from the post body.
```

Image references currently supported:

- Obsidian embeds such as `![[sample.jpg]]`
- Obsidian embeds with width such as `![[sample.jpg|200]]`
- Standard Markdown local images such as `![Alt](../../00_Meta/growth.png)`
- Standard Markdown remote images such as `![Alt](https://example.com/image.png)`

Thumbnails are local vault images. A published note may set `thumbnail` in frontmatter using either a vault-relative path such as `00_Meta/cover.jpg` or a filename such as `cover.jpg`. If `thumbnail` is missing or invalid, the backend uses the first local image reference in the note body. Remote images are never used as thumbnails.

#### Publish Rule

A note is public only if the body contains `#publish`.

Notes without `#publish` are ignored entirely by:

- `/posts`
- `/posts/{slug}`
- `/posts/search`

#### Field Derivation

For each published note, the backend derives fields using these rules:

- `slug`
  - Uses the filename stem.
  - `my-note.md` becomes `my-note`.
- `title`
  - Always uses the filename stem.
  - `my-note.md` becomes title `my-note`.
- `date`
  - Uses frontmatter `date` when it is a valid ISO date.
  - Otherwise falls back to the file modification date.
  - Returned as `YYYY-MM-DD`.
- `tags`
  - Combines frontmatter `tags` with inline Markdown tags like `#ai`.
  - Tags are normalized to lowercase.
  - Duplicate tags are removed.
  - `publish` is used only as a visibility flag and is not returned in API responses.
- `summary`
  - Frontmatter `summary` if present and non-empty.
  - Otherwise the first non-empty body paragraph.
  - Headings and tag-only lines are ignored when building the fallback summary.
  - Long fallback summaries are truncated to 180 characters with `...`.
- `thumbnail_id`
  - Frontmatter `thumbnail` if it resolves to a local vault image.
  - Otherwise the first local image reference in the body.
  - Returned as a vault-relative image path, or `null` when no local thumbnail exists.
- `html`
  - Generated from the Markdown body using `mistune`.
  - Tag-only lines such as `#publish #ai` are removed before rendering.
  - Obsidian image embeds are rewritten to API-served image URLs before rendering.

### Directory Behavior

The loader scans the vault recursively.

That means nested notes are allowed:

```text
vault/
  note-a.md
  nested/
    note-b.md
    deeper/
      note-c.md
```

Any `*.md` file inside the configured vault may become a post if it contains `#publish`.

### API

#### `GET /`

Simple service status message.

Example response:

```json
{
  "message": "Obsidian blog backend is running"
}
```

#### `GET /health`

Health check endpoint.

Example response:

```json
{
  "status": "ok"
}
```

#### `GET /posts`

Returns all published posts as summaries.

Behavior:

- Reads the vault at request time.
- Includes only published notes.
- Sorts posts by date descending.
- Posts without a valid frontmatter date use file modification time for ordering.

Example response:

```json
[
  {
    "title": "with-frontmatter",
    "slug": "with-frontmatter",
    "date": "2026-03-30",
    "tags": ["ai", "notes"],
    "summary": "Frontmatter summary for the list response.",
    "view_count": 12,
    "thumbnail_id": "00_Meta/cover.jpg"
  },
  {
    "title": "hello-world",
    "slug": "hello-world",
    "date": "2026-03-31",
    "tags": ["notes"],
    "summary": "This is the first published post from the stub vault.",
    "view_count": 3,
    "thumbnail_id": null
  }
]
```

#### `GET /posts/{slug}`

Returns one published post including rendered HTML.

Behavior:

- Matches by filename-based slug.
- Returns `404` if the slug does not exist.
- Returns `404` if the file exists but is not published.
- Rewrites local image references in the HTML to `/media/...` URLs.

Example response:

```json
{
  "title": "with-frontmatter",
  "slug": "with-frontmatter",
  "date": "2026-03-30",
  "tags": ["ai", "notes"],
  "summary": "Frontmatter summary for the list response.",
  "view_count": 12,
  "thumbnail_id": "00_Meta/cover.jpg",
  "html": "<p>Hello from the frontmatter-backed post.</p>\n<h2>Heading</h2>\n<p>More body content.</p>\n",
  "comments": [
    {
      "id": 1,
      "post_slug": "with-frontmatter",
      "author_name": "Alice",
      "body": "Nice post.",
      "created_at": "2026-04-03T09:15:00+00:00"
    }
  ]
}
```

#### `POST /posts/{slug}/view`

Increments the stored view count for a published post.

Example response:

```json
{
  "slug": "with-frontmatter",
  "view_count": 13
}
```

#### `POST /posts/{slug}/comments`

Creates a public comment for a published post.

Example request:

```json
{
  "author_name": "Alice",
  "body": "Nice post."
}
```

#### `GET /media/by-name/{asset_name}`

Serves an image referenced by filename, mainly for Obsidian embeds like `![[image.jpg]]`.

Behavior:

- Only image extensions are allowed.
- Looks up the file by basename anywhere in the vault.
- Returns `404` if the image does not exist.

Example:

```text
/media/by-name/Screenshot_20260311_020230_Termux.jpg
```

#### `GET /media/by-path/{asset_path}`

Serves an image by vault-relative path, mainly for standard Markdown image paths.

Behavior:

- Only image extensions are allowed.
- Path traversal is rejected.
- Returns `404` if the image does not exist.

Example:

```text
/media/by-path/00_Meta/growth.png
```

#### `GET /thumbnail/{thumbnail_id}`

Serves the local image identified by a post response `thumbnail_id`.

Behavior:

- `thumbnail_id` is a vault-relative image path.
- Only image extensions are allowed.
- Absolute paths and path traversal are rejected.
- Returns `404` if the image does not exist.

Example:

```text
/thumbnail/00_Meta/cover.jpg
```

#### `GET /posts/search?q=...`

Simple search endpoint over published post summaries.

Behavior:

- Searches only published posts.
- Normal text queries use case-insensitive substring matching across `title`, `summary`, and `tags`.
- `#tag` queries perform exact case-insensitive tag matching.
- Preserves the same date ordering as `/posts`.
- Returns `400` if `q` is missing, blank, or just `#`.
- Returns `[]` if no posts match.

Example request:

```bash
curl "http://127.0.0.1:8000/posts/search?q=notes"
```

Tag search example:

```bash
curl "http://127.0.0.1:8000/posts/search?q=%23todo"
```

Example response:

```json
[
  {
    "title": "Frontmatter Title",
    "slug": "with-frontmatter",
    "date": "2026-03-30",
    "tags": ["ai", "notes"],
    "summary": "Frontmatter summary for the list response."
  }
]
```

Example validation error:

```json
{
  "detail": "Query parameter 'q' is required"
}
```

### Failure Handling

The loader is intentionally permissive and file-based.

Current behavior:

- Missing vault directory:
  - `/posts` returns `[]`
  - `/posts/search` returns `[]`
- Malformed frontmatter:
  - The file is skipped.
  - Other files still load normally.
- Frontmatter that is not a mapping:
  - The file is skipped.
- Invalid dates:
  - The post still loads.
  - The backend falls back to the file modification date.

This keeps one bad note from breaking the whole API response.

### Caching

The backend uses a small in-process cache for parsed posts.

Behavior:

- The cache stores the parsed published post list in memory.
- The cache key is derived from the set of Markdown files in the vault plus each file's modification time.
- If the vault has not changed, `/posts`, `/posts/search`, and `/posts/{slug}` reuse the cached parsed posts.
- If a Markdown file is added, edited, or deleted, the next request rebuilds the cache automatically.

What this means operationally:

- no database is required
- no Redis is required
- each server process keeps its own cache
- there is no shared cache across multiple worker processes
- the first request after a vault change pays the reload cost

### Stub Vault

The repository ships with a small stub vault in [stub_vault](/home/grenade/workspace/tries/2026-03-31-backend/stub_vault) for tests, and the runtime default now points at a git-ignored local copy in `./vault_copy`.

It contains:

- public notes
- private notes
- nested notes
- invalid date examples
- file-time date fallback examples
- notes showing filename-based titles

Use `stub_vault` for regression tests. Use `vault_copy` for local runs against a safe copy of the real vault.

### Test Coverage

The test suite currently covers:

- published vs unpublished filtering
- filename-title and summary fallback rules
- date sorting and undated behavior
- invalid date handling
- file metadata date fallback
- malformed frontmatter skipping
- non-mapping frontmatter skipping
- nested vault traversal
- tag normalization and deduplication
- HTML rendering behavior
- search matching and validation
- missing-vault behavior
- route precedence for `/posts/search`

Tests live in [tests/test_main.py](/home/grenade/workspace/tries/2026-03-31-backend/tests/test_main.py).

### Development Notes

- The backend is intentionally simple and synchronous.
- It uses an in-process parsed-post cache keyed by file path and modification time.
- It refreshes automatically on the next request after vault changes.
- This is appropriate for a small personal blog and keeps the implementation simple.

Current implementation choices:

- Markdown rendering: `mistune`
- Frontmatter parsing: `PyYAML`
- Web framework: `FastAPI`
- App server: `uvicorn`

### Production Notes

The intended production vault path from the original spec is:

```text
/home/grenade/Downloads/sync/khk
```

Recommended deployment shape:

- set `VAULT_DIR` to the real synced vault path
- restrict `CORS_ORIGINS` to the real frontend origin
- place the FastAPI app behind Nginx or Caddy
- keep the API read-only

### Limitations

Current limitations are intentional:

- no database
- no pagination
- no full-text indexing
- no fuzzy search
- no authentication
- no admin interface
- no websocket or SSE updates

This keeps the backend small and predictable.
