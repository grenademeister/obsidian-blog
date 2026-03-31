## Obsidian Blog FastAPI Backend

Install dependencies:

```bash
uv sync
```

Run the server:

```bash
uv run main.py
```

The backend reads notes from a local vault directory and only exposes notes tagged with `#publish`.

Configuration:

```bash
export VAULT_DIR=./stub_vault
export CORS_ORIGINS=https://grenademeister.github.io
```

Defaults:

- `VAULT_DIR` defaults to `./stub_vault` so development does not touch the production vault.
- `CORS_ORIGINS` defaults to `https://grenademeister.github.io`.
- Slugs come from filename stems.
- `summary` comes from frontmatter when present, otherwise from the first body paragraph.

Open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/posts`
- `http://127.0.0.1:8000/posts/search?q=frontmatter`
- `http://127.0.0.1:8000/posts/with-frontmatter`

Search:

- `GET /posts/search?q=...`
- Searches published posts only.
- Matches case-insensitive substrings in `title`, `summary`, and `tags`.
- Returns the same summary objects as `/posts`.
- Returns `400` when `q` is missing or blank.

Run tests:

```bash
uv run pytest -q
```
