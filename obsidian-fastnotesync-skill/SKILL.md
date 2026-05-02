---
name: obsidian-fastnotesync-skill
description: >
  CRUD operations on remote Obsidian notes via Fast Note Sync Service REST API.
  Create, read, update, delete, search, append, prepend, replace, and rename
  Markdown notes stored on a Fast Note Sync server. Use when the user wants to
  manage notes, journal entries, or any Markdown documents in their remote
  Obsidian vault without having Obsidian installed.
license: MIT
compatibility: >
  Requires curl and jq. Needs network access to a
  Fast Note Sync Service instance.
metadata:
  author: https://github.com/zqcli
  version: "1.0.0"
allowed-tools: Bash
---

# Obsidian FastNoteSync Skill

CRUD operations on remote Obsidian notes using the [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) REST API via curl.

## Prerequisites

| Configuration | Required | Description |
|---|---|---|
| `FNS_BASE_URL` | Yes | Fast Note Sync server base URL (e.g. `https://obs6.789210.xyz`) |
| `FNS_USERNAME` | Yes | Login username |
| `FNS_PASSWORD` | Yes | Login password |
| `FNS_VAULT` | No | Default vault name (can be overridden per command) |

Credentials can be set via environment variables or passed inline.

## Authentication

Before any note operation, obtain an auth token by logging in:

```bash
TOKEN=$(curl -s -X POST "${FNS_BASE_URL}/api/user/login" \
  -H "Content-Type: application/json" \
  -d "{\"credentials\":\"${FNS_USERNAME}\",\"password\":\"${FNS_PASSWORD}\"}" \
  | jq -r '.data.token')
```

The token is passed via `Authorization` header on all subsequent requests (note: `Bearer` prefix is required):

```bash
-H "Authorization: Bearer ${TOKEN}"
```

Tokens expire after a period of inactivity (error code `508`). Re-login to obtain a fresh token.

## Response Format

All API responses follow a standard structure:

```json
{
  "code": 1,
  "status": true,
  "message": "success",
  "data": { ... }
}
```

| Field | Description |
|---|---|
| `code` | `0` = failure, `1`+ = success |
| `status` | Boolean operation status |
| `message` | Human-readable message |
| `data` | Business payload (null on failure) |

**Common error codes**: `414` (vault not found), `428` (note not found), `505` (invalid params), `507` (not logged in), `508` (session expired).

---

## Commands

### LIST - List Notes

List notes in a vault with optional search, pagination, and sorting.

```bash
curl -s -X GET "${FNS_BASE_URL}/api/notes?vault=${FNS_VAULT}&page=1&pageSize=20" \
  -H "Authorization: Bearer ${TOKEN}" | jq .
```

**Query Parameters**:

| Param | Required | Default | Description |
|---|---|---|---|
| `vault` | Yes | - | Vault name |
| `keyword` | No | - | Search keyword in path or content |
| `searchContent` | No | false | Search within note content (`true`/`false`) |
| `searchMode` | No | - | `path`, `content`, or `regex` |
| `isRecycle` | No | false | List recycle bin notes |
| `sortBy` | No | `mtime` | Sort field: `mtime`, `ctime`, `path`, `size` |
| `sortOrder` | No | `desc` | Sort order: `asc` or `desc` |
| `page` | No | 1 | Page number |
| `pageSize` | No | 10 | Items per page (max 100) |

**Output**: Paginated list of `NoteNoContentDTO` objects (path, pathHash, size, mtime, etc.) without note body content.

**Example** — list notes containing "journal" in path:
```bash
curl -s "${FNS_BASE_URL}/api/notes?vault=${FNS_VAULT}&keyword=journal&page=1&pageSize=10" \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data.list[] | {path, mtime}'
```

**Example** — search note content:
```bash
curl -s "${FNS_BASE_URL}/api/notes?vault=${FNS_VAULT}&keyword=todo&searchContent=true&page=1" \
  -H "Authorization: Bearer ${TOKEN}" | jq .
```

---

### CREATE - Create or Update a Note

Create a new note or update an existing one. Uses `POST /api/note`.

```bash
curl -s -X POST "${FNS_BASE_URL}/api/note" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Daily/2024-01-01.md\",
    \"content\": \"# Daily Note\n\nToday's entry...\"
  }" | jq .
```

**Request Body**:

| Field | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `path` | Yes | Note file path (e.g. `Notes/ReadMe.md`) |
| `content` | No | Markdown content |
| `pathHash` | No | Path hash (auto-computed if omitted) |
| `contentHash` | No | Content hash for sync |
| `ctime` | No | Creation timestamp in milliseconds |
| `mtime` | No | Modification timestamp in milliseconds |
| `createOnly` | No | If `true`, fails if note already exists |

**Output**: The created/updated `NoteDTO` object.

**Example** — create note from a local file:
```bash
curl -s -X POST "${FNS_BASE_URL}/api/note" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"MyNote.md\",
    \"content\": $(jq -Rs . < local-note.md)
  }" | jq .
```

**Example** — create only if not exists:
```bash
curl -s -X POST "${FNS_BASE_URL}/api/note" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"vault\":\"${FNS_VAULT}\",\"path\":\"NewNote.md\",\"content\":\"# Fresh\",\"createOnly\":true}" | jq .
```

---

### READ - Get a Note

Retrieve a note's full content and metadata.

```bash
curl -s -X GET "${FNS_BASE_URL}/api/note?vault=${FNS_VAULT}&path=Daily%2F2024-01-01.md" \
  -H "Authorization: Bearer ${TOKEN}" | jq .
```

**Query Parameters**:

| Param | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `path` | Yes | Note path (URL-encoded) |
| `pathHash` | No | Alternative to path |
| `isRecycle` | No | Read from recycle bin |

**Output**: `NoteWithFileLinksResponse` containing `content`, `path`, `pathHash`, `contentHash`, `ctime`, `mtime`, `version`, and `fileLinks`.

**Example** — read a note by path hash:
```bash
curl -s "${FNS_BASE_URL}/api/note?vault=${FNS_VAULT}&pathHash=abc123" \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data.content'
```

**Example** — read from recycle bin:
```bash
curl -s "${FNS_BASE_URL}/api/note?vault=${FNS_VAULT}&path=OldNote.md&isRecycle=true" \
  -H "Authorization: Bearer ${TOKEN}" | jq .
```

---

### UPDATE - Update a Note

Update an existing note's full content. Same endpoint as CREATE (`POST /api/note`).

```bash
curl -s -X POST "${FNS_BASE_URL}/api/note" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Daily/2024-01-01.md\",
    \"content\": \"# Updated Daily Note\n\nRevised entry...\"
  }" | jq .
```

To update, omit `createOnly` or set it to `false`. The server identifies the note by `path`/`pathHash` and overwrites its content.

---

### DELETE - Delete a Note

Move a note to the recycle bin (soft delete).

```bash
curl -s -X DELETE "${FNS_BASE_URL}/api/note?vault=${FNS_VAULT}&path=OldNote.md" \
  -H "Authorization: Bearer ${TOKEN}" | jq .
```

**Query Parameters**:

| Param | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `path` | Yes | Note path |
| `pathHash` | No | Alternative to path |

**Output**: Standard response. Note moves to recycle bin — not permanently deleted.

**Restore a deleted note**:
```bash
curl -s -X PUT "${FNS_BASE_URL}/api/note/restore" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"vault\":\"${FNS_VAULT}\",\"path\":\"OldNote.md\"}" | jq .
```

**Permanently clear from recycle bin**:
```bash
curl -s -X DELETE "${FNS_BASE_URL}/api/note/recycle-clear" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"vault\":\"${FNS_VAULT}\",\"path\":\"OldNote.md\"}" | jq .
```
Omit `path` to clear all notes from the recycle bin.

---

### APPEND - Append Content

Append text to the end of an existing note.

```bash
curl -s -X POST "${FNS_BASE_URL}/api/note/append" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Daily/2024-01-01.md\",
    \"content\": \"\n## Evening Update\nAdded later in the day.\"
  }" | jq .
```

**Request Body**:

| Field | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `path` | Yes | Note path |
| `content` | Yes | Text to append |
| `pathHash` | No | Path hash |

---

### PREPEND - Prepend Content

Insert text at the beginning of a note (after YAML frontmatter, if any).

```bash
curl -s -X POST "${FNS_BASE_URL}/api/note/prepend" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Daily/2024-01-01.md\",
    \"content\": \"# Morning Summary\nEarly thoughts.\n\n\"
  }" | jq .
```

---

### REPLACE - Find and Replace

Search and replace text within a note, with optional regex support.

```bash
curl -s -X POST "${FNS_BASE_URL}/api/note/replace" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Daily/2024-01-01.md\",
    \"find\": \"old phrase\",
    \"replace\": \"new phrase\",
    \"all\": true
  }" | jq .
```

**Request Body**:

| Field | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `path` | Yes | Note path |
| `find` | Yes | String or pattern to find |
| `replace` | Yes | Replacement string |
| `all` | No | Replace all occurrences (default: first only) |
| `regex` | No | Treat `find` as regex pattern |
| `failIfNoMatch` | No | Return error if no match found |

**Example** — regex replace all date patterns:
```bash
curl -s -X POST "${FNS_BASE_URL}/api/note/replace" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Log.md\",
    \"find\": \"\\\\[\\\\d{4}-\\\\d{2}-\\\\d{2}\\\\]\",
    \"replace\": \"[REDACTED]\",
    \"regex\": true,
    \"all\": true
  }" | jq .
```

---

### RENAME - Rename a Note

Change a note's file path (can also move to a different folder).

```bash
curl -s -X POST "${FNS_BASE_URL}/api/note/rename" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"oldPath\": \"OldName.md\",
    \"path\": \"NewName.md\"
  }" | jq .
```

**Request Body**:

| Field | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `oldPath` | Yes | Current note path |
| `path` | Yes | New note path |
| `oldPathHash` | No | Current path hash |
| `pathHash` | No | New path hash |

---

### FRONTMATTER - Update Frontmatter

Modify or remove YAML frontmatter fields without touching the note body.

```bash
curl -s -X PATCH "${FNS_BASE_URL}/api/note/frontmatter" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"vault\": \"${FNS_VAULT}\",
    \"path\": \"Project.md\",
    \"updates\": {\"status\": \"done\", \"tags\": [\"important\"]},
    \"remove\": [\"draft\"]
  }" | jq .
```

**Request Body**:

| Field | Required | Description |
|---|---|---|
| `vault` | Yes | Vault name |
| `path` | Yes | Note path |
| `updates` | No | Key-value map of fields to add/update |
| `remove` | No | Array of field names to delete |

---

### SEARCH - Full-Text Search

Search note content across the vault (via the LIST endpoint with `searchContent=true`).

```bash
curl -s -X GET "${FNS_BASE_URL}/api/notes?vault=${FNS_VAULT}&keyword=meeting&searchContent=true" \
  -H "Authorization: Bearer ${TOKEN}" | jq '.data.list[] | {path, mtime}'
```

---

## Important Notes

- **Case sensitivity**: Note paths are case-sensitive. `ReadMe.md` and `readme.md` are different notes.
- **URL encoding**: Always URL-encode note paths in query parameters (e.g. `/` → `%2F`, spaces → `%20`).
- **Timestamps**: All timestamps (`ctime`, `mtime`) are Unix timestamps in **milliseconds**. Use `date +%s000` to generate current timestamp.
- **Token expiry**: Session tokens expire after inactivity. Re-login if you receive error code `507` (not logged in) or `508` (session expired).
- **Soft delete**: `DELETE /api/note` moves notes to the recycle bin. Use `/api/note/recycle-clear` for permanent deletion.
- **Concurrency**: The `contentHash` and `baseHash` fields support conflict detection. The server returns the latest `contentHash` after each write — store it for subsequent updates.
- **Vault auto-creation**: Vaults are created automatically on first use. No explicit vault creation needed for note operations.
- **JSON escaping**: When passing content with special characters (quotes, backslashes, newlines), use `jq -Rs .` to properly escape file contents.

## Edge Cases

| Scenario | Behavior |
|---|---|
| Note not found (READ/UPDATE/DELETE) | Error code `428`, message "Note does not exist" |
| Vault not found | Error code `414`, message "Note Vault does not exist" |
| Session expired (idle timeout) | Error code `508`, re-login required |
| Not authenticated (no/missing token) | Error code `507`, message "Not logged in" |
| Invalid JSON body | Error code `505`, message "Invalid Params" |
| Empty content on CREATE | Note created with empty body — succeeds |
| Path with special characters | URL-encode the path; server handles slashes as folder separators |
| Duplicate path (createOnly=true) | Error — note already exists |
