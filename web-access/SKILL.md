---
name: web-access
description: >
  Web search and readable content extraction for AI agents. Search the web via
  the Brave Search API (with date/freshness filtering) and fetch any URL as clean
  Markdown (boilerplate, scripts, and nav stripped). Use for looking up docs,
  facts, current events, or reading a specific page without browser noise.
license: MIT
compatibility: >
  Requires Node.js >= 18 and a one-time `npm install` in this skill dir.
  Web search needs a BRAVE_API_KEY. http/https proxy supported (no SOCKS).
allowed-tools: Bash
metadata:
  author: https://github.com/zqcli
  version: "0.1.0"
  runtime: node
  category: web
---

# Web Access

Two Node scripts under `scripts/`:

- **`search.js`** — Brave web search; optionally fetches each result as Markdown.
- **`read.js`** — fetch one URL and return readable Markdown (or raw body).

Both extract content with Readability + Turndown (no browser; JavaScript on the
page is NOT executed, so purely client-rendered SPAs may return little — use the
page's API or a server-rendered URL when that happens).

## Setup (one-time)

```bash
cd {baseDir}
npm install
```

Web search also needs a Brave API key (free tier; card required to register but
not charged). Add to your shell profile:

```bash
export BRAVE_API_KEY="your-key"   # https://api-dashboard.search.brave.com
```

## Search

```bash
node scripts/search.js "rust async runtime"                 # 5 results
node scripts/search.js "node fetch proxy" -n 10             # up to 20
node scripts/search.js "openai news" --freshness pw         # last week
node scripts/search.js "ai policy" --freshness 2025-01-01to2025-06-30
node scripts/search.js "deno kv" --content                  # results + page markdown
node scripts/search.js "berlin" --country DE
node scripts/search.js "x" --content --proxy http://127.0.0.1:7890
```

| Option | Description |
|---|---|
| `-n`, `--count <num>` | Results, default 5, max 20 |
| `--country <code>` | Two-letter country code, default US |
| `--freshness <period>` | **Date filter**: `pd` (day), `pw` (week), `pm` (month), `py` (year), or range `YYYY-MM-DDtoYYYY-MM-DD` |
| `--content` | Fetch each result page as Markdown (serial, rate-limit friendly) |
| `--max-chars <n>` | Truncate per-page content, default 4000 |
| `--proxy <url>` | `http://` / `https://` proxy (or `HTTPS_PROXY` env) |

Output:

```
## 1. <title>
<url> · <age>
<snippet>

<markdown content, if --content>

---

## 2. ...
```

## Read a URL

```bash
node scripts/read.js https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html
node scripts/read.js https://example.com --max-chars 8000
node scripts/read.js https://api.github.com/repos/nodejs/node --raw   # JSON/API passthrough
node scripts/read.js https://example.com --proxy http://127.0.0.1:7890
```

| Option | Description |
|---|---|
| `--raw` | Print the raw body (for JSON / non-HTML APIs) |
| `--max-chars <n>` | Truncate output, default no limit |
| `--timeout <sec>` | Request timeout, default 15 |
| `--proxy <url>` | `http://` / `https://` proxy (or `HTTPS_PROXY` env) |

Non-HTML responses are passed through verbatim even without `--raw`.

## Notes

- **Date filtering** is the Brave `freshness` parameter — pass it to `search.js`.
- **Proxy**: only `http://` / `https://` (routed via undici `ProxyAgent`); SOCKS is not supported.
- **No JS execution**: extraction is on server-delivered HTML. SPAs that render
  client-side may yield little content; prefer their API or `--raw`.
- **Failures surface directly**: a missing `BRAVE_API_KEY`, non-2xx response, or
  unextractable page returns a clear error on stderr with a non-zero exit.
