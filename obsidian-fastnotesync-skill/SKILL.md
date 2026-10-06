---
name: obsidian-fastnotesync-skill
description: >
  Manage remote Obsidian Markdown notes through the Fast Note Sync Service REST
  API using scripts/fns.py. Use for vault discovery, listing, path or content
  search, reading, creating, updating, upserting, soft deletion, restoration,
  append, prepend, replacement, frontmatter edits, renaming, and recycle clearing
  without a local Obsidian installation.
license: MIT
compatibility: >
  Requires Python 3.10+ on POSIX (macOS/Linux), requests[socks]>=2.34.2,<3,
  urllib3>=2.8.0,<3, and network access to Fast Note Sync Service. Tests use
  Python's standard library test framework and the same runtime dependencies.
metadata:
  author: https://github.com/zqcli
  version: "2.0.1"
allowed-tools: Bash
---

# Obsidian FastNoteSync Skill

Use `python3 scripts/fns.py` for note operations, vault discovery, and diagnostics.
Resolve script and reference paths relative to this skill directory, not the
caller's working directory. Examples below run from the skill directory using
the configured Python environment. `scripts/fns.sh` remains a thin compatible
legacy launcher requiring POSIX `sh` and forwarding arguments to Python, not a
separate HTTP transport.

## Setup

Python 3.10+ on POSIX (macOS/Linux), `requests[socks]>=2.34.2,<3`, and
`urllib3>=2.8.0,<3` are required. The urllib3 minimum supports an independent
HTTPS-proxy certificate policy, including requests to HTTP destinations.
Install all dependencies together from skill-root `requirements.txt`; no
separate manual networking-library install, Bash/curl/jq/iconv, or runtime SDK
is required by the Python entry point. Use a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
. .venv/bin/activate
```

Activation makes the examples' `python3` use the installed dependencies. You may
instead invoke `.venv/bin/python` directly. Keep secrets out of setup commands.

The REST contract is frozen to official upstream tag **3.6.1**, commit
`7a6c78792c631f999c8a5f725bba5dd7235d6688`, published **August 14, 2026**.
Implemented behavior takes precedence over Swagger and API comments. Consult
[references/api-contract.md](references/api-contract.md) for endpoint mappings,
source links, response handling, and concurrency limitations.

## Configuration and authentication

| Variable | Required / default | Meaning |
|---|---|---|
| `FNS_BASE_URL` | Required for network commands | Server base URL, without `/api`, credentials, secret query parameters, or URL-glob expressions in its authority. Prefer HTTPS. |
| `FNS_TOKEN` | Required for authenticated commands in default token mode | Existing token supplied through a trusted environment or secret manager. |
| `FNS_VAULT` | Required for note commands unless `--vault V` is supplied | Name of an existing vault. |
| `FNS_PROXY` | Optional | HTTP/HTTPS/SOCKS proxy URL: `socks5://`, `socks5h://`, `http://`, or `https://`; authenticated URLs are supported through the environment only. |
| `FNS_CLIENT` | `obsidian-fastnotesync-skill` | `x-client` in token mode; must match the token's permissions. |
| `FNS_USER_AGENT` | `obsidian-fastnotesync-skill/2.0.1` | Stable User-Agent for every request. |
| `FNS_TIMEOUT` | `30` | Total wall-clock deadline in seconds per request, enforced with POSIX `SIGALRM`, not an inactivity timeout. |
| `FNS_CONNECT_TIMEOUT` | `10` | Additional cap on connection time in seconds, within the total deadline. |
| `FNS_ALLOW_HTTP` | Unset | Must be exactly `1` to permit non-loopback plain HTTP. |
| `FNS_AUTH_MODE` | `token` | Only explicit `password` selects compatibility login. |
| `FNS_USERNAME` / `FNS_PASSWORD` | Required only in password mode | Login credentials provided through the environment, not command arguments. |

**Upgrade compatibility:** the default User-Agent changes from
`obsidian-fastnotesync-skill/2.0.0` to `obsidian-fastnotesync-skill/2.0.1`.
To reuse a manual token bound to the old default, securely configure the
non-secret `FNS_USER_AGENT='obsidian-fastnotesync-skill/2.0.0'`, or provision a
new token externally. Never automatically rotate or replace credentials.

Provision secrets outside the conversation using the user's existing secret
manager or protected environment. **Never ask users to paste tokens or passwords.**
Keep service tokens and login credentials out of URLs. An authenticated
`FNS_PROXY` may contain proxy credentials, but must be provisioned through the
environment, never a CLI option or inline shell assignment. Do not print
environment dumps or include any credentials in shell history, process arguments,
examples, logs, diagnostics, or responses.

Non-secret configuration example; authentication is deliberately omitted:

```bash
export FNS_BASE_URL='https://notes.example.com'
export FNS_VAULT='Personal'
export FNS_PROXY='socks5h://127.0.0.1:1080'  # Optional; DNS resolves through the proxy.
python3 scripts/fns.py doctor
```

`FNS_PROXY` accepts authenticated HTTP/HTTPS/SOCKS URLs, including proxy
username/password userinfo, through the environment only. Requests uses the
value in memory, never a process argument or credential file. `socks5://`
resolves destination DNS locally; `socks5h://` resolves it through the proxy.
An explicit `FNS_PROXY` maps both HTTP and HTTPS requests to that proxy and
overrides inherited proxy settings and `NO_PROXY` / `no_proxy` exclusions.
Without it, the client manually selects standard environment proxies using
`requests.utils.get_environ_proxies`, including applicable bypass rules.
HTTP/HTTPS proxy Basic authentication percent-decodes userinfo to bytes before
Base64 encoding, preserving UTF-8 credential bytes rather than using Requests'
Latin-1 default.

The Requests session has `trust_env=False` to disable `.netrc` authentication
that could overwrite the Bearer header. The client separately honors
`REQUESTS_CA_BUNDLE`, then `CURL_CA_BUNDLE`, using certifi by default with TLS
verification always enabled; never use `verify=False`. A custom adapter supplies
an explicit CA- and hostname-verifying `proxy_ssl_context` for HTTPS proxies,
independent of destination TLS policy, even for HTTP destinations. This does not
encrypt the proxy-to-HTTP-origin hop. Loopback HTTP, such as
`http://127.0.0.1:9000`, needs no opt-in; non-loopback plain HTTP requires
`FNS_ALLOW_HTTP=1` and still exposes traffic in transit.

### Default token mode

Authenticated commands require `FNS_TOKEN`; missing tokens do not trigger login
or a fallback to passwords. Send it only as an `Authorization: Bearer` header.
Public `doctor`, `health`, and `version` do not require a token or selected vault
and do not perform login. `vaults` requires authentication but no selected vault.

### Explicit password compatibility mode

Use `FNS_AUTH_MODE=password` only when compatibility login is intended and
`FNS_USERNAME` / `FNS_PASSWORD` are already securely provisioned. Login uses
`POST /api/user/login` with JSON fields `credentials` and `password`.
**Both login and subsequent business requests must use `x-client: webgui`**, not
`FNS_CLIENT`, and the same stable `FNS_USER_AGENT`. Login-issued tokens are bound
to client identity and may also be bound to User-Agent or IP.

Keep the login token within the current invocation only. There is **no token
cache, automatic refresh, re-login on failure, or automatic retry** in either
mode. On expiry or rejection, report the error; obtain replacement credentials
through the approved external workflow, never through chat.

### Transport and secret handling

Requests/PySocks handles HTTP directly, with structured query parameters and
JSON bodies, not subprocess curl. Tokens, login payloads, and proxy credentials
stay in memory, never in process arguments, persistent caches, or sensitive
temporary files. URL-glob expressions in the base URL authority remain rejected;
there is no curl URL-globbing transport.

No logging/tracing, redirect following, retries, automatic token refresh, or
failure-triggered re-login. Do not enable shell tracing (`set -x`) or HTTP debug
logs. Error messages are intentionally generic to prevent upstream echo leaks.
Normal JSON output and diagnostics redact known credentials; see below.
`FNS_TIMEOUT` is a POSIX `SIGALRM` wall-clock deadline for each request, including
environment/platform proxy selection, request JSON serialization, body reading,
and response JSON validation, not merely Requests' connect/read inactivity
timeout. `FNS_CONNECT_TIMEOUT` additionally caps connection time.

## CLI conventions

```text
python3 scripts/fns.py [--vault V] COMMAND [OPTIONS]
```

`--vault V` may appear anywhere in the arguments and overrides `FNS_VAULT`.
All note commands require a resolved vault. Commands addressing a single note
require a nonempty path even when `--path-hash H` is supported; explicit full
recycle clearing is the exception. Quote paths and text; the script handles
URL encoding and JSON serialization. Transport URL encoding does not guarantee
server path acceptance: upstream `ValidatePath` uses `QueryUnescape` and rejects
bare/unescaped `%` with `444` on validated paths. The client must not silently
rewrite a requested filename to contain literal `%25` as a workaround; that is
a different filename. See the pinned validator in the API contract.
`ctime` and `mtime` values are Unix timestamps in milliseconds.

Literal `--content`, content-file, and stdin note content must be valid UTF-8.
Python strictly validates UTF-8; files/stdin are read as bytes and decoded without
newline translation. Invalid UTF-8 is rejected before login or any write, not
silently replaced or transcoded. Valid content retains CRLF and final newlines,
without normalization. This does not imply byte preservation by server-side
edits that reserialize YAML.

`--help` emits unstructured text. Normal command output is JSON; consume the
script envelope rather than assuming the raw REST envelope is unchanged.

### Discovery and diagnostics

| Command | Purpose |
|---|---|
| `doctor` | Check local prerequisites/configuration and public health + version endpoints; not an authentication, vault-access, or write test. |
| `health` | Public `GET /api/health`. |
| `version` | Public `GET /api/version`. |
| `vaults` | Authenticated `GET /api/vault` to discover available vaults. |

Vaults must already exist. This CLI does not create, update, delete, or otherwise
manage vaults; upstream vault mutations are WebGUI-only. Missing-vault failures
may use `420`, or `0` when wrapped by the POST precheck; do not assume every
operation preserves `420`.

### List and search

```text
list   [--keyword K] [--search-mode path|content] [--page N]
       [--page-size N] [--sort-by mtime|ctime|path] [--sort-order asc|desc]
       [--recycle]
search --keyword K [--search-mode path|content] [--page N]
       [--page-size N] [--sort-by mtime|ctime|path] [--sort-order asc|desc]
       [--recycle]
```

`search` requires a nonempty keyword and defaults to `content`; `list` defaults
to `path`. Page numbers are positive integers; page size is **1..100**.
`--recycle` selects recycled notes. Both commands use `GET /api/notes` and return
metadata without note content, with pagination in `data.pager` and entries in
`data.list`. For **list/search only**, the client normalizes upstream
`data.list: null` to `[]` when `data.pager.totalRows` is exactly `0`. A null list
with a nonzero or missing total count is rejected as a protocol failure (exit 4),
not accepted as empty. This normalization does not apply to other commands.

Full-text search actually runs only when `searchMode=content` **and Bleve is
enabled on the server**; otherwise upstream falls back to path matching. Do not
claim results establish a content search without that server configuration.
`searchContent` is ignored. Regex list/search and size sorting are not supported.

```bash
python3 scripts/fns.py list --keyword 'Daily/' --page 1 --page-size 20
python3 scripts/fns.py search --keyword 'meeting' --sort-by mtime --sort-order desc
python3 scripts/fns.py list --vault 'Archive' --recycle
```

### Read

```text
read --path P [--path-hash H] [--recycle]
```

Read returns note content, hashes, timestamps, version, and `fileLinks` metadata.
`fileLinks` is not an attachment lifecycle API: reading or editing a note does
not upload, rename, restore, or delete its embedded files.

```bash
python3 scripts/fns.py read --path 'Daily/Entry.md'
python3 scripts/fns.py --vault 'Archive' read --path 'Old.md' --recycle
```

### Create, update, and upsert

```text
create --path P CONTENT_SOURCE [--ctime MS] [--mtime MS] [--path-hash H]
update --path P CONTENT_SOURCE [--ctime MS] [--mtime MS] [--path-hash H]
       [--expect-hash H]
upsert --path P CONTENT_SOURCE [--ctime MS] [--mtime MS] [--path-hash H]
```

Select **exactly one** `CONTENT_SOURCE`: `--content TEXT`, `--content-file FILE`,
or `--stdin`. Empty text, an empty file, or empty stdin is valid for these three
commands and intentionally writes an empty note. Omitting a source is a usage
error, even though the raw POST endpoint treats omitted content as empty.
All sources follow the UTF-8 validation and newline-preservation rules above.

- `create` always sends `createOnly: true`; an active existing note yields `431`.
  Upstream can reuse a soft-deleted record even with `createOnly: true`.
- `upsert` sends `createOnly: false`; it may create, overwrite, or revive a
  soft-deleted note. It is not a strict update operation.
- `update` first GETs the active note to enforce existence at the precheck,
  retains its `ctime` unless `--ctime` is explicitly supplied, then POSTs the
  full replacement content with `createOnly: false`.
- `--expect-hash H` compares the GET's `contentHash` before update and fails with
  exit 6 on mismatch without writing. It is a **precheck only, not atomic CAS**.

REST `baseHash` is not checked. Another writer can change or delete the note
between GET and POST; update can then overwrite newer content, recreate, or
revive the note. There is no reliable strict update-only guarantee under
concurrent deletion. Re-read before deciding how to handle an uncertain result;
never silently retry a write.

```bash
python3 scripts/fns.py create --path 'Draft.md' --content ''
python3 scripts/fns.py update --path 'Draft.md' --content-file './draft.md'
printf '%s\n' '# Imported note' | python3 scripts/fns.py upsert --path 'Import.md' --stdin
```

### Specialized edits

```text
append      --path P CONTENT_SOURCE
prepend     --path P CONTENT_SOURCE
replace     --path P --find STR --replace STR [--regex] [--all]
            [--fail-if-no-match]
frontmatter --path P [--updates JSON_OBJECT] [--remove JSON_ARRAY_OF_STRINGS]
```

Append and prepend use the same exactly-one content-source syntax. The upstream
specialized endpoints require nonempty content and reject empty text with `305`;
empty full-note writes remain valid via create/update/upsert.
Frontmatter requires at least one of `--updates` (a JSON object) or `--remove`
(a JSON array of strings); build structured JSON, not interpolated shell text.
Nonfinite JSON numbers, including overflow such as `1e999`, are rejected locally
as usage errors before login or a write.

These endpoints perform server-side **read-modify-write, not atomic edits**;
concurrent changes can be lost. Append adds text literally, without an automatic
separator. Prepend inserts text after recognized YAML frontmatter. Prepend and
frontmatter parse and reserialize YAML, so comments, formatting, ordering, and
line endings are not byte-preserved. Frontmatter updates are shallow; removal
runs after updates and wins when both mention a key.

Replace defaults to the first literal match. `--regex` uses **Go regexp**, not
PCRE. In regex mode, `all: false` inserts the replacement literally, whereas
`all: true` expands capture references such as `${1}`. Quote them to prevent
shell expansion. `--replace ''` is valid; `--find` must be nonempty. Without
`--fail-if-no-match`, no match is successful; with it, upstream returns `442`.
Invalid regex returns `443`. `matchCount` counts matches found, not necessarily
the number replaced.

```bash
printf '\n## Update\nReviewed.\n' | python3 scripts/fns.py append --path 'Daily/Entry.md' --stdin
python3 scripts/fns.py prepend --path 'Daily/Entry.md' --content 'Summary: '
python3 scripts/fns.py replace --path 'Log.md' --find 'old phrase' --replace 'new phrase' --all
python3 scripts/fns.py replace --path 'Log.md' --find '(item)' --replace '${1}-done' --regex --all
python3 scripts/fns.py frontmatter --path 'Project.md' --updates '{"status":"done"}' --remove '["draft"]'
```

### Rename, delete, restore, and recycle clearing

```text
rename        --old-path P --path Q [--old-path-hash H] [--path-hash H]
delete        --path P
restore       --path P
recycle-clear --path P --confirm
recycle-clear --all --confirm-vault EXACT_VAULT
```

Rename requires both old and new paths even if hashes are supplied. `delete`
is soft deletion; `restore` restores a note from the recycle bin.
Before clearing, inspect `list --recycle` and establish the intended scope.
For a single note, require a nonempty path and `--confirm`. Full clearing requires
`--all` and `--confirm-vault` exactly matching the resolved vault, with **no path
or hash options**. `--confirm` alone never authorizes full clearing. These CLI
guards are local; the raw REST endpoint has no confirmation mechanism.

Recycle clearing marks recycled records with `rename = 2`, excluding them from
ordinary recycle queries; it is **not physical erasure or secure deletion**.
Full-clear requests leave both `path` and `pathHash` empty. Do not describe clear
as an attachment cleanup or guarantee that stored data has been erased.

```bash
python3 scripts/fns.py rename --old-path 'Old.md' --path 'Archive/Old.md'
python3 scripts/fns.py delete --path 'Draft.md'
python3 scripts/fns.py restore --path 'Draft.md'
# Only after the requested scope has been reviewed and authorized:
python3 scripts/fns.py recycle-clear --path 'Draft.md' --confirm
python3 scripts/fns.py --vault 'Archive' recycle-clear --all --confirm-vault 'Archive'
```

## Output and failures

Every normal command returns one credential-redacted JSON envelope:

```json
{"success":true,"code":1,"data":{"path":"Draft.md"}}
```

```json
{"success":false,"error":{"type":"business","message":"Upstream business error","code":430,"httpStatus":200}}
```

Success always contains `success`, `code`, and `data` (`null` when absent).
Failure always contains `success` and `error.type` / `error.message`;
`error.code` and `error.httpStatus` preserve the business code and HTTP status
when known. Error messages are intentionally generic, not the original server
message; the example illustrates the envelope, not an exact message guarantee.
Help is text, not this envelope. Never expose raw login payloads or error bodies.

Redaction covers known tokens, usernames, passwords, authenticated proxy URLs,
and proxy credential components, including URL-encoded and decoded forms.
It applies recursively to payload string values and object keys, including
success data. Client envelope keys (`success`, `code`, `data`, `error`, and error
metadata) remain stable even if a credential equals `data`. **Stdout redaction
can change matching secret text in returned note content; it does not change
content sent to or stored by the server.**

| Exit | Meaning |
|---|---|
| `0` | Success. |
| `2` | Configuration or usage failure. |
| `3` | Transport failure or unsuccessful HTTP status. |
| `4` | Malformed JSON or invalid response protocol. |
| `5` | Upstream business failure, including errors inside HTTP 200. |
| `6` | Local precondition failure, such as an expected-hash mismatch. |

Only upstream codes **1..6** are success codes. Business errors may arrive with
HTTP 200 and with or without `status` / `data`; never treat HTTP 200 or an absent
`status` as success by itself. Important codes: `305` invalid parameters;
`307` missing token; `308` invalid token; `310` expired token; `314` client
restriction; `315` scope/vault-access restriction; `420` missing vault;
`430` missing note; `431` existing note; `442` no match; `443` invalid regex;
`444` invalid path. Report them without automatic retry or credential refresh.

## Scope and verification

This lightweight CLI covers the note operations above, vault discovery, and
diagnostics. History, backlinks/outlinks, attachment lifecycle, and vault
management are **out of scope for this release**. Any future additions require
a separate scope decision; response link/file metadata does not add those
commands to the current interface.

Tests use Python 3.10+'s standard library test framework with the installed
runtime dependencies, not curl/mock-curl fixtures. Use the same Python
environment for commands and tests. From the repository root, run:

```bash
# Syntax check without execution or bytecode-cache files:
python3 -B - <<'PY'
import ast
from pathlib import Path
path = Path('obsidian-fastnotesync-skill/scripts/fns.py')
compile(ast.parse(path.read_bytes(), filename=str(path)), str(path), 'exec')
PY
python3 -B -m unittest discover -s obsidian-fastnotesync-skill/tests -v
# Optional legacy-launcher compatibility check, not a runtime requirement:
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck obsidian-fastnotesync-skill/scripts/fns.sh
fi
if command -v skills-ref >/dev/null 2>&1; then
  skills-ref validate obsidian-fastnotesync-skill
fi
```

Report documentation checks, script syntax/lint checks, unit tests, and live
tests separately; offline checks do not establish live-server compatibility.

### Opt-in live tests

**Live write tests require explicit authorization and an isolated test vault.**
Use `FNS_AUTH_MODE=token`, securely provisioned `FNS_BASE_URL` / `FNS_TOKEN`, and
an existing `FNS_VAULT` named `fns-skill-test-*`, preferably with a token scoped
to that vault. Create the dedicated vault outside this CLI. From the repository
root, manually opt in with an exact-vault confirmation:

```bash
python3 -B obsidian-fastnotesync-skill/tests/run_live.py --confirm-vault "$FNS_VAULT"
```

The runner invokes `scripts/fns.py` with its current Python executable, so its
environment must contain the installed runtime dependencies. Add optional
`--allow-clear` only when recycle-clear tests are authorized. Current-run UUID
path guards constrain fixture operations; the runner retains active fixtures
and the vault, rather than performing complete teardown.
Full clearing requires an initially empty vault and verification that every
recycled path belongs to the current run's UUID scope. These are **non-atomic
prechecks**: ensure there are no concurrent writers throughout the run.
Keep all tests within that dedicated vault, never ordinary user notes. Do not
include private instance URLs, actual vault names, credentials, live note data,
or run-specific results in this documentation.
