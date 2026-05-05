---
name: bitwarden-skill
description: >
  Manage Bitwarden/Vaultwarden vault items via the official bw CLI.
  Supports batch URI resolution (bw://"Item"/field), CRUD on logins,
  notes, cards, and identities with unified JSON output.
license: MIT
compatibility: >
  Requires Bitwarden CLI (bw). Compatible with official Bitwarden and
  Vaultwarden self-hosted servers.
allowed-tools: Bash
metadata:
  author: https://github.com/zqcli
  version: "0.5.0"
---

# Bitwarden Skill

All vault operations go through `scripts/bw.sh` — a self-contained wrapper that handles auto-unlock, base64 encoding, and unified JSON output.

## Prerequisites

| Requirement | Description |
|---|---|
| `bw` CLI | [Bitwarden CLI](https://bitwarden.com/help/cli/) installed and in `PATH` |
| `jq` | JSON processor (for `bw.sh` output parsing) |
| Vault Host | Your Bitwarden/Vaultwarden server URL |
| Master Password | Passed via `-p` flag or `BW_PASSWORD` env var each invocation |

## Setup (One-Time)

`bw.sh` auto-handles login, unlock, and session renewal. On first use, just pass `-e` with your email:

```bash
bash scripts/bw.sh -e you@example.com -p 'masterpwd' 'bw://"Item"/password'
```

This will `bw login` + `bw unlock` automatically. Subsequent invocations only need `-p`:

```bash
bash scripts/bw.sh -p 'masterpwd' 'bw://"Item"/password'
```

If you prefer to set up manually:

```bash
bw config server https://vault.example.com
bw login you@example.com
```

## Quick Start

```bash
# First use (auto login + unlock)
bash scripts/bw.sh -e you@example.com -p 'masterpwd' 'bw://"My Server"/password'

# Subsequent uses (auto unlock only)
bash scripts/bw.sh -p 'masterpwd' list --search "github"
bash scripts/bw.sh -p 'masterpwd' get "My Server" --field username
```

**All output is JSON**: `{"success":true,"data":...}` or `{"success":false,"error":"..."}`.

---

## Command Reference

### Global Options

All subcommands support these flags before the subcommand name:

| Flag | Description |
|---|---|
| `-e`, `--email <email>` | Login email — required if not yet logged in (or set `BW_EMAIL` env) |
| `-p`, `--password <pwd>` | Master password (or set `BW_PASSWORD` env var) |
| `--proxy <url>` | Route all bw traffic through proxy: `socks5://`, `socks5h://`, `http://`, `https://` |
| `-s`, `--sync` | Sync vault before reading (for `resolve`, `list`, `get`, `folders`) |
| `--no-unlock` | Skip session setup — use when `BW_SESSION` is already valid |
| `-h`, `--help` | Show help |

---

### resolve — Batch `bw://` URI Resolution

**No flags.** Positional: one or more `bw://"Item"/field` URIs. This is the **default subcommand** — you can omit `resolve`.

**URI format**: `bw://"Item Name"/{field}`

| Field Path | Resolves To |
|---|---|
| `/password` | `login.password` |
| `/username` | `login.username` |
| `/totp` | Live 6-digit TOTP code |
| `/notes` | `notes` |
| `/uri` | `login.uris[0].uri` |
| `/fields/<name>` | Custom field value |

**Example — single URI:**

```bash
bash scripts/bw.sh -p '***' 'bw://"GitHub"/password'
```

Return:

```json
{"success":true,"data":{"GitHub":{"password":"***"}}}
```

**Example — multiple URIs, auto-merged by item name:**

```bash
bash scripts/bw.sh -p '***' \
  'bw://"API Gateway"/password' \
  'bw://"API Gateway"/fields/api_key' \
  'bw://"DB Server"/username'
```

Return:

```json
{"success":true,"data":{
  "API Gateway":{"password":"***","api_key":"sk-***"},
  "DB Server":{"username":"dbadmin"}
}}
```

**Example — item not found:**

```bash
bash scripts/bw.sh -p '***' 'bw://"Missing"/password'
```

Return:

```json
{"success":true,"data":{"Missing":{"password":"NOT FOUND"}}}
```

---

### list — List Vault Items

| Flag | Description |
|---|---|
| `--search <term>` | Substring match on name/URI |
| `--type <n>` | Filter by type: `1`=Login, `2`=Note, `3`=Card, `4`=Identity |
| `--folder <id>` | Filter by folder ID |
| `--trash` | List trashed items only |

**Example:**

```bash
bash scripts/bw.sh -p '***' list --search "aws" --type 1
```

Return:

```json
{"success":true,"data":[
  {"id":"abc-123","name":"AWS Console","type":1,"folderId":"f-id","username":"admin@example.com"},
  {"id":"def-456","name":"AWS IAM","type":1,"folderId":"f-id","username":"iam-user"}
]}
```

---

### get — Get Single Item

Positional: `<id|name>` — exact name match, case-sensitive.

| Flag | Description |
|---|---|
| `--field <f>` | Extract single field: `password`, `username`, `totp`, `notes`, `uri` |

**Example — get by name:**

```bash
bash scripts/bw.sh -p '***' get "GitHub" --field password
```

Return:

```json
{"success":true,"data":"***"}
```

**Example — get by name, full item (no `--field`):**

```bash
bash scripts/bw.sh -p '***' get "GitHub"
```

Return:

```json
{"success":true,"data":{"id":"abc-123","name":"GitHub","type":1,"login":{"username":"user","password":"***","uris":[{"uri":"https://github.com"}]},...}}
```

**Example — TOTP code:**

```bash
bash scripts/bw.sh -p '***' get "Ivanti" --field totp
```

Return:

```json
{"success":true,"data":"123456"}
```

---

### create — Create Item

**No flags.** Positional: JSON string, file path, or `-` for stdin.

Base64 encoding is handled internally. Item types: `1`=Login, `2`=Secure Note, `3`=Card, `4`=Identity.

**Example — Login (JSON string):**

```bash
bash scripts/bw.sh -p '***' create \
  '{"type":1,"name":"My App","login":{"username":"user","password":"secret","uris":[{"uri":"https://app.example.com"}]}}'
```

Return:

```json
{"success":true,"data":{"id":"new-uuid","name":"My App","type":1,"folderId":null}}
```

**Example — Secure Note:**

```bash
bash scripts/bw.sh -p '***' create \
  '{"type":2,"name":"Server Notes","notes":"DB: 10.0.0.1\nPass: ***","secureNote":{"type":0}}'
```

**Example — Card:**

```bash
bash scripts/bw.sh -p '***' create \
  '{"type":3,"name":"Visa","card":{"cardholderName":"John","brand":"Visa","number":"4111111111111111","expMonth":"12","expYear":"2028","code":"123"}}'
```

**Example — from file:**

```bash
bash scripts/bw.sh -p '***' create /path/to/item.json
```

**Example — from stdin:**

```bash
echo '{"type":1,"name":"Piped","login":{"username":"u","password":"p","uris":[]}}' \
  | bash scripts/bw.sh -p '***' create -
```

---

### edit — Edit Item

**No flags.** Positional: `<id> <jq-patch>`. Uses a **jq patch expression** to modify fields. The script fetches the full item, applies the jq expression, base64-encodes, and sends the update.

Requires **item id** (use `list` or `get` to find it first).

**Example — change name and password:**

```bash
bash scripts/bw.sh -p '***' edit abc-123 \
  '.name="New Name" | .login.password="newSecret"'
```

Return:

```json
{"success":true,"data":{"id":"abc-123","name":"New Name","revisionDate":"2025-01-01T00:00:00.000Z"}}
```

**Example — move to folder:**

```bash
bash scripts/bw.sh -p '***' edit abc-123 '.folderId="folder-uuid"'
```

**Example — toggle favorite:**

```bash
bash scripts/bw.sh -p '***' edit abc-123 '.favorite=true'
```

---

### delete — Delete Item

**No flags.** Positional: `<id|name>`.

```bash
bash scripts/bw.sh -p '***' delete abc-123
bash scripts/bw.sh -p '***' delete "My Old Item"
```

Return:

```json
{"success":true,"data":{"deleted":true,"id":"abc-123"}}
```

First delete moves to trash; second delete permanently removes.

---

### folders — List Folders

**No flags.**

```bash
bash scripts/bw.sh -p '***' folders
```

Return:

```json
{"success":true,"data":[
  {"id":"f1","name":"Personal"},
  {"id":"f2","name":"Personal/Finance"},
  {"id":"f3","name":"Work"}
]}
```

### folders — Create Folder (raw bw CLI)

Folder creation is not wrapped in `bw.sh`. Use the raw `bw` CLI:

```bash
echo -n "New Folder" | bw encode | xargs -0 bw create folder
```

### lock — Lock Vault

**No flags.** Locks the vault (clears the decryption key from memory).

```bash
bash scripts/bw.sh lock
```

Return:

```json
{"success":true,"data":"locked"}
```

### logout — Log Out

**No flags.** Logs out completely, clearing local credentials. Requires `-e` + `-p` on next use to re-login.

```bash
bash scripts/bw.sh logout
```

Return:

```json
{"success":true,"data":"logged out"}
```

---

## Item Type Reference

| Type | Value | Description |
|---|---|---|
| Login | `1` | Username, password, URI, TOTP |
| Secure Note | `2` | Free-form text |
| Card | `3` | Card number, expiry, CVV, cardholder |
| Identity | `4` | Name, address, email, phone |

---

## Important Notes

- **Three-state session handling**: `bw.sh` detects `unauthenticated` → logs in, `locked` → unlocks, `unlocked` → skips. No manual `bw login` or `bw unlock` needed.
- **`-e` only needed once**: After `bw login` succeeds, subsequent invocations only need `-p`. Passing `-e` again is harmless — it won't re-login.
- **Password via `-p` or env**: Pass master password with `-p` flag or `export BW_PASSWORD=...`. Also works with `export BW_EMAIL=...`.
- **Unified JSON output**: All subcommands return `{"success":true,"data":...}` or `{"success":false,"error":"..."}`. Parse with `jq`.
- **Exact name matching**: `resolve`, `get`, and `delete` match item names exactly (case-sensitive).
- **resolve is default**: Running `bw.sh` with `bw://` URIs and no subcommand automatically uses `resolve`.
- **resolve returns NOT FOUND, not null**: Missing items/fields return `"NOT FOUND"` string — prevents silent failures in pipelines.
- **Base64 handled internally**: `create` and `edit` handle base64 encoding transparently. Pass plain JSON or jq expressions.
- **First delete is soft**: Items go to trash on first delete. Second delete on the same item removes permanently.
- **Proxy via `--proxy`**: Set `--proxy socks5h://127.0.0.1:1080` to route all bw network traffic through a proxy. Supports `socks5://` (local DNS), `socks5h://` (remote DNS), `http://`, and `https://`. Sets `HTTP_PROXY`/`HTTPS_PROXY` environment variables.

## Error Reference

| Output | Cause | Solution |
|---|---|---|
| `{"success":false,"error":"BW_PASSWORD not set..."}` | No `-p` flag or `BW_PASSWORD` env | Add `-p 'password'` |
| `{"success":false,"error":"bw unlock failed..."}` | Wrong master password | Check password |
| `{"success":false,"error":"item not found: X"}` | Item doesn't exist (or name mismatch) | Check exact name with `list --search` |
| `{"success":false,"error":"invalid JSON"}` | Malformed JSON in create | Validate with `jq empty` |
| `{"success":false,"error":"unknown subcommand: X"}` | Typo in subcommand name | Use `-h` for help |

## Edge Cases

| Scenario | Behavior |
|---|---|
| Session expired mid-operation | `bw.sh` auto-reunlocks |
| Item name contains `"` or `/` | `bw://` parsing handles it via awk field splitting |
| Password contains special chars (`$`, `\`, `"`) | JSON-safe; `bw.sh` uses `printf` not `echo` |
| Create duplicate name | Allowed (Bitwarden doesn't enforce uniqueness) |
| Empty vault | `list` returns `{"success":true,"data":[]}` |
| Delete non-existent item | `{"success":false,"error":"item not found: X"}` |
| `--no-unlock` without valid session | `bw` prompts for password interactively (may hang in non-TTY) |
