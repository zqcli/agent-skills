---
name: bitwarden-skill
description: >
  Manage Bitwarden/Vaultwarden vault items (passwords, notes, cards, identities)
  via the Bitwarden CLI (bw). Create, read, update, delete, and search vault
  entries including logins, secure notes, credit cards, and identity records.
  Use when the user wants to manage passwords, retrieve credentials, store
  secrets, or query their vault.
license: MIT
compatibility: >
  Requires Bitwarden CLI (bw) installed and configured.
  Compatible with Bitwarden official and Vaultwarden self-hosted servers.
allowed-tools: Bash
metadata:
  author: https://github.com/zqcli
  version: "0.2.0"
---

# Bitwarden Skill

Manage Bitwarden/Vaultwarden vault items via the official `bw` CLI. All data is decrypted client-side — the skill returns plaintext values.

## Prerequisites

| Requirement | Description |
|---|---|
| `bw` CLI | [Bitwarden CLI](https://bitwarden.com/help/cli/) must be installed and in `PATH` |
| Vault Host | Provide the Vaultwarden/Bitwarden server URL |
| Master Password | User must **type it manually each session** (never stored or hardcoded) |

## Setup (One-Time)

Configure the server endpoint and log in. This persists your account credentials locally.

```bash
# Configure the server
bw config server https://vault.example.com

# Log in (stores encrypted credentials locally, permanent until logout)
bw login your-email@example.com
```

**Check current status** at any time:

```bash
bw status | jq .
# { "serverUrl": "https://...", "status": "unlocked", "userEmail": "..." }
```

## Authentication (Per Session)

Each session requires unlocking with your master password. The session token expires after a configurable period (default 12 hours).

### Step 1: Unlock

The user is prompted to type their master password:

```bash
export BW_SESSION=$(bw unlock --raw)
# User types master password at the interactive prompt
```

Verify unlock succeeded:

```bash
bw status | jq '.status'   # Should output "unlocked"
```

### Step 2: Use the session

All subsequent `bw` commands automatically use `BW_SESSION`. No need to re-unlock until expiry.

### Session Expiry

If commands fail with `not logged in` or `session expired`:

```bash
export BW_SESSION=$(bw unlock --raw)
```

---

## Sync Before Read

**Always sync before read operations** to ensure data is up-to-date with the server:

```bash
bw sync
```

This pulls the latest vault state from the server. Skip sync for write-only operations (create/edit/delete) since those talk to the server directly.

---

## CRUD Operations

All commands below assume `BW_SESSION` is exported and `bw sync` has been run before reads.

### LIST — List All Items

```bash
bw sync && bw list items | jq .
```

**Filter options**:

```bash
# List only logins
bw list items --type 1 | jq .

# Search by name or URI (case-insensitive substring match)
bw list items --search "github" | jq .

# List items in a folder
bw list items --folderid <folder-id> | jq .

# List only trashed items
bw list items --trash | jq .
```

**Shorthand** — print a compact summary table:

```bash
bw list items | jq -r '.[] | "\(.id) | \(.name) | \(.login.username // "-")"'
```

### READ — Get a Single Item

```bash
bw sync && bw get item <item-id> | jq .
```

**Convenience shortcuts** — extract specific fields:

```bash
bw get password <item-id>   # Returns password only
bw get username <item-id>   # Returns username only
bw get totp <item-id>       # Returns TOTP code (6-digit, time-based)
bw get notes <item-id>      # Returns notes only
bw get uri <item-id>        # Returns the primary URI
```

### CREATE — Create an Item

Item types: `1` = Login, `2` = Secure Note, `3` = Card, `4` = Identity.

**JSON must be base64-encoded** before passing to `bw create item`. Use the pipe pattern: `echo '<json>' | base64 | bw create item`.

**Login (type 1)**:

```bash
echo '{"type":1,"name":"GitHub","notes":"Personal account","favorite":true,"login":{"username":"user@example.com","password":"mySecret123","uris":[{"uri":"https://github.com","match":null}]}}' | base64 | bw create item
```

Or write JSON to a file for readability:

```bash
cat > /tmp/item.json << 'JSON'
{
  "type": 1,
  "name": "GitHub",
  "notes": "Personal account",
  "favorite": true,
  "login": {
    "username": "user@example.com",
    "password": "mySecret123",
    "uris": [{"uri": "https://github.com", "match": null}]
  }
}
JSON
base64 /tmp/item.json | bw create item
```

**Secure Note (type 2)**:

```bash
echo '{"type":2,"name":"Server Access","notes":"SSH key passphrase: hunter2\nIP: 10.0.0.1","secureNote":{"type":0}}' | base64 | bw create item
```

**Card (type 3)**:

```bash
echo '{"type":3,"name":"Visa Card","card":{"cardholderName":"John Doe","brand":"Visa","number":"4111111111111111","expMonth":"12","expYear":"2028","code":"123"}}' | base64 | bw create item
```

**Identity (type 4)**:

```bash
echo '{"type":4,"name":"John Doe","identity":{"title":"Mr","firstName":"John","lastName":"Doe","email":"john@example.com","phone":"1234567890","address1":"123 Main St","city":"Springfield","state":"IL","postalCode":"62701","country":"US"}}' | base64 | bw create item
```

**Create in root folder** (omit `folderId` or set it to `null`):

```bash
echo '{"type":1,"name":"Root Item","folderId":null,"login":{"username":"user","password":"pass","uris":[]}}' | base64 | bw create item
```

### UPDATE — Edit an Item

`bw edit item` requires the **full item JSON** (not just the changed fields) and must be **base64-encoded**. The recommended pattern: get the current item, modify with `jq`, pipe to `base64`, then edit.

```bash
bw get item <item-id> | jq '.name = "New Name" | .notes = "Updated notes"' | base64 | bw edit item <item-id>
```

**Update password** (the password field is inside the `login` object):

```bash
bw get item <item-id> | jq '.login.password = "newPassword"' | base64 | bw edit item <item-id>
```

**Move to a folder**:

```bash
bw get item <item-id> | jq '.folderId = "<folder-id>"' | base64 | bw edit item <item-id>
```

**Toggle favorite**:

```bash
bw get item <item-id> | jq '.favorite = true' | base64 | bw edit item <item-id>
```

### DELETE — Delete an Item

```bash
bw delete item <item-id>
```

**Restore from trash**:

```bash
bw restore item <item-id>
```

---

## Folder Management

### List Folders

```bash
bw sync && bw list folders | jq -r '.[] | "\(.id) | \(.name)"'
```

### Create Folder

```bash
echo -n "Folder Name" | bw encode | xargs -0 bw create folder
```

### Delete Folder

```bash
bw delete folder <folder-id>
```

---

## Item Type Reference

| Type | Value | CLI filter | Description |
|---|---|---|---|
| Login | `1` | `--type 1` | Username, password, URI, TOTP |
| Secure Note | `2` | `--type 2` | Free-form text note |
| Card | `3` | `--type 3` | Credit/debit card details |
| Identity | `4` | `--type 4` | Personal information |

---

## Important Notes

- **Master password is never stored** — the user must type it at `bw unlock` each session.
- **BW_SESSION is temporary** — expires after inactivity (default 12h). If commands start failing, re-run `bw unlock`.
- **Sync before reads** — always run `bw sync` before `bw get` or `bw list` to get the latest server data.
- **Write operations go directly to the server** — `bw create`, `bw edit`, `bw delete` do not need a prior `bw sync`.
- **bw login persists** — credentials are stored locally and survive reboots. Only need to re-login after `bw logout` or on a new machine.
- **JSON payloads must be base64-encoded** — `bw create item` and `bw edit item` require base64-encoded JSON input. Use the pattern: `echo '<json>' | base64 | bw create item` or `bw get item <id> | jq '...' | base64 | bw edit item <id>`.
- **Full object required for edit** — `bw edit item` needs the complete item JSON, not partial fields. Always pipe through `bw get item <id>` first, modify with `jq`, then edit.
- **Exit codes** — `bw` returns 0 on success, non-zero on failure. Check `$?` after critical commands.

## Error Codes

| Exit Code | Cause | Solution |
|---|---|---|
| 1 | Not logged in | Run `bw login` |
| 1 | Session expired | Run `bw unlock --raw` and export `BW_SESSION` |
| 1 | Item not found | Check item ID with `bw list items` |
| 1 | Invalid JSON | Validate JSON syntax; use `jq` to format |
| 1 | Server unreachable | Check vault host URL and network connectivity |

## Edge Cases

| Scenario | Behavior |
|---|---|
| Session expired during operation | Command fails. Re-run `bw unlock` and retry. |
| Item already in trash (delete twice) | Second delete permanently removes the item. |
| Create with duplicate name | Allowed (Bitwarden does not enforce unique names). |
| Empty vault | `bw list items` returns `[]`. |
| TOTP field empty | `bw get totp <id>` returns nothing or error. |
| Password contains special characters (quotes, `$`, `\`) | Use single-quoted JSON strings or `bw encode` for safety. |
| Network timeout during sync | `bw sync` times out; retry. |
