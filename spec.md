
## Simple Spec Sheet: Obsidian Blog with FastAPI + GitHub Pages

### Goal

Publish selected Obsidian notes as blog posts with near-real-time updates. Markdown files already exist on a home server and are synced there via Syncthing.

### Overall Architecture

```text
Obsidian
  -> Syncthing
    -> Home Server Vault Folder
      -> FastAPI Backend
        -> JSON API for post list / post detail
          -> GitHub Pages Frontend
            -> Reader's browser
```

IMPORTANT: Your job is to only make the FastAPI backend part.
this folder has a minimal FastAPI server code, so make it based on it.
commit your work regularly.

### Components

**1. Obsidian Vault**

* Source of truth
* Notes written in Markdown
* Optional YAML frontmatter

markdown notes look like this:

title: title.md
```md
#tag1 #tag2 (optional)

Hello
## Heading

```


**2. Syncthing**

* Syncs vault to home server
* No manual push or deploy step

**3. FastAPI Backend**

* Reads markdown files from synced vault directory
* Filters only `published: true`
* Parses frontmatter
* Converts markdown to HTML
* Exposes simple API

**4. GitHub Pages Frontend**

* Static frontend only
* Fetches post list and post content from FastAPI
* Renders blog pages

### Minimal API Spec

**GET `/posts`**

* Returns all published posts
* Response:

```json
[
  {
    "title": "My Post",
    "slug": "my-post",
    "date": "2026-03-30",
    "tags": ["ai", "notes"],
    "summary": "Short preview"
  }
]
```

**GET `/posts/{slug}`**

* Returns one rendered post
* Response:

```json
{
  "title": "My Post",
  "slug": "my-post",
  "date": "2026-03-30",
  "tags": ["ai", "notes"],
  "html": "<p>Hello world</p>"
}
```

### Update Model

* FastAPI reads files directly from disk
* Frontend refetches on page load
* No rebuild required

### Publish Rule

A note is public only if:

* it has `#publish` tag

Use both if you want stricter control.

### Directory Example

`/home/grenade/Downloads/sync/khk` will be used in production
you should make and use a stub folder during development in order to protect my files.

### Recommended Simplifications

* No database initially
* No admin panel
* No WebSocket initially
* No authentication for writing, because files are edited locally in Obsidian
* Only read-only public API

Simple and minimal as possible

### Security Notes

* Expose only published posts
* Never serve the full vault directly
* Restrict CORS to your GitHub Pages domain
* Put FastAPI behind Nginx or Caddy

### Tech Choices

* **Backend**: FastAPI
* **Markdown parsing**: `python-frontmatter` + `markdown-it-py` or `mistune`
* **Frontend**: Vite/React or plain HTML/JS
* **Sync**: Syncthing

### MVP Scope

* Post list page
* Individual post page
* Frontmatter-based publishing
* Markdown-to-HTML rendering
* Tag display
* Date sorting

### Nice-to-Have Later

* Full-text search
* RSS feed
* Sitemap
* Syntax highlighting
* SSE/WebSocket for instant refresh
* Caching layer

### Decision Summary

The simplest viable system is:

* Obsidian writes notes
* Syncthing syncs them
* FastAPI reads and serves them
* GitHub Pages frontend displays them

 no build pipeline, minimal moving parts.
