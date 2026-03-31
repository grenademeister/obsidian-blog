## Obsidian Blog FastAPI Backend

Small FastAPI backend for publishing selected Obsidian notes as a read-only blog API.

The service reads Markdown files directly from a vault directory on disk, filters to public notes, converts Markdown to HTML, and exposes simple JSON endpoints for a static frontend.

### What It Does

- Reads `*.md` files from a configured vault directory.
- Parses optional YAML frontmatter.
- Publishes only notes containing `#publish`.
- Returns post summaries through `/posts`.
- Returns rendered post content through `/posts/{slug}`.
- Provides simple substring search through `/posts/search?q=...`.
- Uses direct disk reads on each request. No database, cache, or build step.

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
export VAULT_DIR=./stub_vault
export CORS_ORIGINS=https://grenademeister.github.io
```

Available settings:

- `VAULT_DIR`
  - Path to the Markdown vault to read from.
  - Default: `./stub_vault`
  - The default exists to protect the real production vault during development.
- `CORS_ORIGINS`
  - Comma-separated list of allowed origins.
  - Default: `https://grenademeister.github.io`
  - Example: `http://localhost:5173,https://grenademeister.github.io`

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
---

#publish #research

# My Post

Hello from the post body.
```

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
- `html`
  - Generated from the Markdown body using `mistune`.
  - Tag-only lines such as `#publish #ai` are removed before rendering.

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
    "summary": "Frontmatter summary for the list response."
  },
  {
    "title": "hello-world",
    "slug": "hello-world",
    "date": "2026-03-31",
    "tags": ["notes"],
    "summary": "This is the first published post from the stub vault."
  }
]
```

#### `GET /posts/{slug}`

Returns one published post including rendered HTML.

Behavior:

- Matches by filename-based slug.
- Returns `404` if the slug does not exist.
- Returns `404` if the file exists but is not published.

Example response:

```json
{
  "title": "with-frontmatter",
  "slug": "with-frontmatter",
  "date": "2026-03-30",
  "tags": ["ai", "notes"],
  "summary": "Frontmatter summary for the list response.",
  "html": "<p>Hello from the frontmatter-backed post.</p>\n<h2>Heading</h2>\n<p>More body content.</p>\n"
}
```

#### `GET /posts/search?q=...`

Simple search endpoint over published post summaries.

Behavior:

- Searches only published posts.
- Uses case-insensitive substring matching.
- Searches `title`, `summary`, and `tags`.
- Preserves the same date ordering as `/posts`.
- Returns `400` if `q` is missing or blank.
- Returns `[]` if no posts match.

Example request:

```bash
curl "http://127.0.0.1:8000/posts/search?q=notes"
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

### Stub Vault

The repository ships with a development stub vault in [stub_vault](/home/grenade/workspace/tries/2026-03-31-backend/stub_vault).

It contains:

- public notes
- private notes
- nested notes
- invalid date examples
- file-time date fallback examples
- notes showing filename-based titles

Use it for local development and regression testing instead of pointing at the production vault.

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
- It does not cache parsed notes.
- It reparses the vault on every request.
- This is appropriate for a small personal blog and easy to reason about.

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
- no caching
- no websocket or SSE updates

This keeps the backend small and predictable.
