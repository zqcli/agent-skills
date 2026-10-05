# FastNoteSync 2.0.0 API Contract

## Frozen source and authority

This contract targets the official `haierkeys/fast-note-sync-service` repository:

| Item | Frozen value |
|---|---|
| Upstream tag | `3.6.1` |
| Commit | `7a6c78792c631f999c8a5f725bba5dd7235d6688` |
| Release publication | August 14, 2026 (`2026-08-14T17:15:46Z`) |
| Skill version | `2.0.0` |
| CLI entry point | `scripts/fns.sh`, relative to the skill directory |
| Runtime | Bash 3.2+, curl, jq, standard Unix iconv, and standard shell utilities |
| Test runtime | Python 3.8+ standard library (`unittest`); no Python runtime dependency |

The [official release][release] and [release metadata][release-api] identify the
pin. All implementation links below use the full commit, not a moving branch.
**Implemented routes, request validation, middleware, services, repositories,
and serializers override Swagger, generated schemas, and descriptive comments.**
Do not assume a different server version implements the same behavior.

There are two distinct layers: the script interface described in `SKILL.md`,
and the pinned REST behavior below. CLI input validation, update prechecks,
credential redaction, and recycle confirmations are script safeguards, not
extra guarantees provided by the server.

## Configuration, authentication, and transport

The primary variables are `FNS_BASE_URL`, `FNS_TOKEN`, and `FNS_VAULT`.
`--vault V` overrides the vault anywhere in the CLI arguments. Note operations
require an existing vault; discovery does not require a selected vault.
`doctor`, `health`, and `version` are public and need neither a token nor a vault.

| Setting | Contract |
|---|---|
| `FNS_AUTH_MODE` | Defaults to `token`; `password` must be selected explicitly. |
| `FNS_TOKEN` | Required for authenticated token-mode commands; never implicit password fallback. |
| `FNS_USERNAME`, `FNS_PASSWORD` | Required only for explicit password compatibility mode. |
| `FNS_CLIENT` | Defaults to `obsidian-fastnotesync-skill`; token-mode `x-client`. |
| `FNS_USER_AGENT` | Defaults to `obsidian-fastnotesync-skill/2.0.0`; stable across requests. |
| `FNS_PROXY` | Optional native curl proxy URL using `socks5`, `socks5h`, `http`, or `https`; authenticated URLs are supported via environment and private curl config only. |
| `FNS_TIMEOUT` | Defaults to 30 seconds per request. |
| `FNS_CONNECT_TIMEOUT` | Defaults to 10 seconds per connection attempt. |
| `FNS_ALLOW_HTTP` | Exactly `1` is required for non-loopback plain HTTP. HTTPS is preferred. |

Protected requests use an `Authorization: Bearer` header. Although upstream can
extract a token from other headers or a query, this CLI must not put service
tokens or login credentials in URLs. Supply secrets through a trusted
environment/secret manager only, never through CLI arguments or inline shell
assignments; never ask users to paste secrets or show raw credentials.
`FNS_BASE_URL` must not contain credentials, username/password userinfo, secret
query parameters, or URL-glob expressions in its authority. Curl URL globbing is
disabled (`globoff`) for requests. **`FNS_PROXY` does accept proxy authentication userinfo** in
native HTTP/HTTPS/SOCKS URLs. Provision authenticated proxy URLs through the
environment only and write their value to the private curl config, never a
`--proxy` process argument. Keep proxy credentials and authenticated URLs out
of shell history, examples, logs, diagnostics, and error messages. Do not disable
TLS verification. HTTP opt-in does not encrypt data.

Password compatibility login is `POST /api/user/login`, with `credentials`
(from `FNS_USERNAME`) and `password` in a JSON body. Login and every subsequent
business request use **`x-client: webgui`**, overriding `FNS_CLIENT`, and exactly
the same stable User-Agent. Upstream requires the WebGUI identity on login and
checks client, User-Agent, IP, scope, and vault restrictions on protected
requests. Setting a WebGUI header on a manual token does not grant WebGUI-only
management access. See [routes][routes], [login handler][login-handler],
[login DTO][login-dto], [token authentication][auth], and [WebGUI gate][webgui].

No token cache, automatic refresh, failure-triggered re-login, or automatic retry
is permitted. A compatibility login token is used only within that invocation;
any private curl-config files are temporary and cleaned up, not a persistent
cache. Expired or rejected tokens are reported without a hidden second attempt.
`doctor` probes only public health and version, not protected access.

Curl must read secret headers, authenticated proxy configuration, and login-body
files via a config in a private temporary directory: `umask 077`, directory mode
700, file mode 600, and cleanup on exit. Secret contents must not appear in
curl/jq arguments. No shell tracing, curl verbose/trace output, redirect
following, or automatic retry. Error messages are intentionally generic to
prevent upstream or curl echo leaks. Normal JSON output and diagnostics redact
known credentials and authenticated proxy URLs, including success payloads;
see the response contract below.

Proxy configuration is one native curl value, not shell-split; `socks5` resolves
destination DNS locally and `socks5h` through the proxy. When `FNS_PROXY` is set,
the private curl config sets `noproxy` to an empty string, forcing routing through
the explicit proxy and overriding inherited `NO_PROXY` / `no_proxy` exclusions.
When `FNS_PROXY` is unset, curl retains its native proxy-environment behavior.

## CLI and REST mapping

All routes below are relative to `FNS_BASE_URL`. Authenticated routes are
protected by token middleware. JSON bodies use `Content-Type: application/json`;
query values must be encoded structurally. [Route registration][routes] is the
authority for HTTP methods and visibility.

| CLI command | REST request | Input / behavior |
|---|---|---|
| `doctor` | `GET /api/health`, `GET /api/version` | Public diagnostics; one script result, no login or business-write probe. |
| `health` | `GET /api/health` | Public health envelope. |
| `version` | `GET /api/version` | Public server version envelope. |
| `vaults` | `GET /api/vault` | Authenticated vault discovery; no selected vault required. |
| `list`, `search` | `GET /api/notes` | Query: `vault`, `keyword`, `searchMode`, `page`, `pageSize`, `sortBy`, `sortOrder`, `isRecycle`. |
| `read` | `GET /api/note` | Query: `vault`, required `path`, optional `pathHash`, `isRecycle`. |
| `create` | `POST /api/note` | Body: `vault`, `path`, `content`, `createOnly: true`; optional `pathHash`, `ctime`, `mtime`. |
| `upsert` | `POST /api/note` | Same body with `createOnly: false`. |
| `update` | `GET /api/note`, then `POST /api/note` | Require an active note at GET, preserve its `ctime` unless explicitly overridden; POST uses `createOnly: false`. |
| `delete` | `DELETE /api/note` | Query: `vault`, `path`; soft delete. |
| `restore` | `PUT /api/note/restore` | Body: `vault`, `path`; restore from recycle bin. |
| `append` | `POST /api/note/append` | Body: `vault`, `path`, `content`. |
| `prepend` | `POST /api/note/prepend` | Body: `vault`, `path`, `content`; insert after recognized frontmatter. |
| `replace` | `POST /api/note/replace` | Body: `vault`, `path`, `find`, `replace`, `regex`, `all`, `failIfNoMatch`. |
| `frontmatter` | `PATCH /api/note/frontmatter` | Body: `vault`, `path`, optional `updates`, `remove`. |
| `rename` | `POST /api/note/rename` | Body: `vault`, `oldPath`, `path`, optional `oldPathHash`, `pathHash`. |
| `recycle-clear` | `DELETE /api/note/recycle-clear` | JSON body: `vault`, and single-note `path` or empty `path` / `pathHash` for full clearing. |

Wire fields do not automatically create CLI switches. This lightweight CLI
covers note operations, vault discovery, and diagnostics. History,
backlinks/outlinks, attachment lifecycle, and vault management are outside this
release; any future additions require a separate scope decision.

### Approved input rules

- `--vault V` is a global option allowed anywhere. `FNS_VAULT` is the fallback.
- `list` accepts `--keyword`, defaults to path search, and supports
  `--search-mode path|content`, `--page N`, `--page-size N` (1..100),
  `--sort-by mtime|ctime|path`, `--sort-order asc|desc`, and `--recycle`.
  `search` has the same options but requires a nonempty keyword and defaults to
  content search. Page numbers must be positive integers.
- `read --path P` supports optional `--path-hash H` and `--recycle`.
- Create/update/upsert require `--path P` and exactly one of `--content TEXT`,
  `--content-file FILE`, or `--stdin`, including an explicitly empty source.
  Optional `--ctime MS`, `--mtime MS`, and `--path-hash H` are supported.
  Only update accepts `--expect-hash H`, a precheck of the GET's `contentHash`.
- `append --path P` and `prepend --path P` require exactly one of the same content
  sources. Upstream's specialized DTOs reject empty content with `305`; this is
  distinct from the valid empty content of full-note create/update/upsert.
- `replace --path P --find STR --replace STR` accepts `--regex`, `--all`, and
  `--fail-if-no-match`. Find is nonempty; an explicitly empty replacement is valid.
- `frontmatter --path P` requires at least one of `--updates` (JSON object) or
  `--remove` (JSON array of strings); arbitrary JSON scalars are not valid.
- `rename --old-path P --path Q` supports `--old-path-hash H` and `--path-hash H`.
- `delete --path P` and `restore --path P` operate on one note.
- Single clearing requires `recycle-clear --path P --confirm`. Full clearing
  requires `recycle-clear --all --confirm-vault EXACT_VAULT`, where the string
  exactly matches the resolved vault. Full clearing rejects all path/hash
  options. Neither an omitted path nor `--confirm` alone requests a full clear.
- Help is unstructured text; normal command responses use the JSON envelope below.

These input rules intentionally constrain the raw REST interface. The
[upstream DTOs][note-dto] require nonempty `path` on addressed note operations
even when `pathHash` is supplied; rename likewise requires both paths. Hashes
are optional selectors, not substitutes for paths. Full recycle clearing is the
explicit exception. `ctime` and `mtime` are Unix milliseconds, not seconds.

### Path acceptance

Transport URL encoding is not a guarantee of server path acceptance. Upstream
[ValidatePath][path-validation] calls `QueryUnescape`; bare/unescaped `%` fails
validation, and the [POST handler][note-handler] returns `444`. The client must
not silently rewrite a requested filename to contain literal `%25` as a
workaround: it denotes a different filename, not transparent transport encoding.

### Content encoding and preservation

Literal `--content`, `--content-file`, and stdin note content must be valid UTF-8.
Standard Unix `iconv` is a required runtime dependency for validation. Invalid
UTF-8 is rejected locally before login or any write; it is not silently replaced
or transcoded. Valid UTF-8 input is not normalized, and input handling neither
adds nor removes trailing newlines. This applies to the content sources for
create/update/upsert and append/prepend; it does not promise that server-side
YAML reserialization preserves the existing note's bytes.

## Response normalization and exit contract

Script success has exactly the common fields `success`, `code`, and `data`:

```json
{"success":true,"code":1,"data":null}
```

Script failure contains an error object; `code` and `httpStatus` are included
only when known:

```json
{"success":false,"error":{"type":"business","message":"Upstream business error","code":430,"httpStatus":200}}
```

Error messages are intentionally generic to prevent upstream echo leaks; they
do not preserve the original server message or error body. Preserve the upstream
business code and HTTP status in `error.code` / `error.httpStatus` when known.
The example illustrates the envelope, not an exact generic-message guarantee.
Never output raw login responses or upstream error bodies.

For successful replies, retain the upstream success code and credential-redacted
payload; normalize an omitted payload to `data: null`. Redaction covers known
tokens, usernames, passwords, authenticated proxy URLs, and proxy credential
components, including URL-encoded and decoded forms. Apply it recursively to
JSON string values and object keys, including success data. **Stdout redaction
can change matching secret text in returned note content; it does not change
content sent to or stored by the server.** Do not expose an upstream failure as
success merely because curl completed or returned HTTP 200.

For **list/search only**, upstream's nil-slice empty result `data.list: null`
is normalized to `[]` only when `data.pager.totalRows` is exactly `0`. Null with
a nonzero or missing total count is a protocol failure (exit 4), not an empty
result. Other commands' null values are not covered by this normalization.
Consumers can iterate the normalized empty array without special nil handling.

| Exit | Category |
|---|---|
| `0` | Successful command. |
| `2` | Local configuration or usage error. |
| `3` | Transport failure or unsuccessful HTTP status. |
| `4` | Malformed JSON or invalid response protocol. |
| `5` | Valid upstream business failure. |
| `6` | Local precondition failure, including expected-hash mismatch. |

A non-success HTTP status is an HTTP failure even if its body is JSON. A
successful HTTP status still requires a valid application response. Parse the
business code and any supplied status; do not require `status` or `data` to be
present on errors. Malformed JSON or a non-envelope response is a protocol
failure, not a successful empty result.

The [standard response serializer][response] emits `code`, `status`, optional
`message`, `data`, and `details`. The [application error serializer][errors]
emits `code`, `message`, and other diagnostic fields without necessarily
including `status` or `data`. [Code.StatusCode][code-implementation] returns
HTTP 200, and the application error serializer also uses HTTP 200. Thus valid
business errors can have either shape, for example:

```json
{"code":305,"status":false,"message":"Invalid parameters","data":null}
```

```json
{"code":430,"message":"Note not found"}
```

Neither shape should be mistaken for a transport error or accepted as success.
These are illustrative envelopes, not promises of exact upstream message text.

### Pinned business codes

The [code declarations][codes] define **only 1..6 as success codes**; a positive
integer in general is not a success. Individual handlers commonly return `1`
even for writes, so do not infer a create-versus-update outcome from the number.

| Code | Meaning |
|---|---|
| `1` | Generic success. |
| `2` | Create success. |
| `3` | Update success. |
| `4` | Delete success. |
| `5` | Password-update success. |
| `6` | No-update success. |
| `0` | Generic business failure. |
| `305` | Invalid parameters. |
| `307` | Missing user token. |
| `308` | Invalid user token. |
| `310` | Expired token. |
| `312` / `313` | Token IP / User-Agent restriction. |
| `314` | Client restriction (including WebGUI gate or login-token client mismatch). |
| `315` | Scope, protocol/client permission, or vault-access restriction. |
| `414` | User blocked by an administrator. |
| `420` | Vault not found. |
| `430` | Note not found. |
| `431` | Note already exists, including an occupied rename destination. |
| `442` | No replacement match when failure on no match is requested. |
| `443` | Invalid Go regular expression. |
| `444` | Invalid note path. |

Local validation can reject bad configuration/input before any request; it does
not invent an upstream code. A local hash precondition failure is likewise not a
server CAS conflict. Vault lookup uses `420`, but the POST precheck may wrap it
as generic `0`; `414` is an administrative user block, not a missing-vault alias.
Earlier `428` missing-note or `507`/`508` authentication descriptions do not apply
to this frozen contract.

## Behavioral corrections and limitations

### Vault existence and management

Note services call `VaultService.MustGetID`; a missing vault is not auto-created.
Lookup returns `420`, but the [POST handler's precheck][note-handler] may wrap
that failure as generic `0`; not all operations preserve `420`.
Authenticated `GET /api/vault` supports discovery; mutating vault routes remain
WebGUI-only and are not exposed by this CLI. See [vault lookup][vault-service]
and [route registration][routes].

### List/search is narrower than earlier documentation

The [note repository][note-repository] selects content/full-text search only
when there is a keyword, `searchMode == "content"`, and Bleve is enabled. The
Bleve query matches paths and content; it is not a regex list filter. Without
Bleve, content-mode requests fall back to SQL path matching. `searchContent` is
accepted by the DTO but does not drive the repository's search decision.

Only `mtime`, `ctime`, and `path` sorting is implemented; unsupported raw sort
fields fall back to `mtime`. The script does not offer `size` sorting or
`searchMode=regex`. Raw pagination defaults to page 1, size 10, with a maximum
of 100; explicit CLI page sizes must stay within 1..100 rather than depending on
server clamping. See [pagination][pagination] and [note DTOs][note-dto].
List output contains `list` and `pager` (`page`, `pageSize`, `totalRows`) inside
the payload, with metadata-only entries.

### POST, timestamps, and non-atomic update

`POST /api/note` is an upsert unless `createOnly` is true. Omitted raw `content`
is the empty string and can erase an existing body. The script therefore
requires an explicit source even for empty notes. Create-only rejects active
notes, but the [service][note-service] can reuse soft-deleted records, including
with `createOnly: true`. Upsert can revive a soft-deleted note.

The [POST handler][note-handler] supplies missing/zero `mtime` from current time
and missing/zero `ctime` from `mtime`; the service assigns the supplied `ctime`
even when modifying an existing note. Thus update must read and preserve the
existing `ctime` unless the user explicitly provides it. When content changes,
the handler can replace a supplied `mtime` with server current time; timestamp
options are not a guarantee that the server will retain every supplied value.

The REST DTO includes `baseHash`, but this REST handler/service path **does not
check it**. It provides **no compare-and-swap (CAS)**. `--expect-hash` compares
`contentHash` from a prior GET only, and returns a local precondition failure
without POST on mismatch. It cannot prevent a later concurrent write or delete.
An update GET can establish existence only at that moment: subsequent POST may
overwrite newer content, recreate, or revive a deleted note. There is no reliable
strict update-only behavior under concurrent deletion.

### Specialized edits are read-modify-write

Append, prepend, replace, and frontmatter first fetch content and later call
`ModifyOrCreate`; these are **not atomic edits**. Serializing writes within the
service does not make the preceding read and transformation atomic. Lost
updates remain possible. See [edit implementations][note-service].

Append concatenates content without adding a separator. Prepend parses YAML and
inserts text before the body, after recognized frontmatter. Prepend and
frontmatter reserialize YAML, so formatting, comments, key order, and line
endings are not byte-preserved. Frontmatter updates are shallow assignments;
removal runs afterward, so **remove wins**. Invalid/unrecognized frontmatter is
treated as body text rather than an opaque frontmatter block. See the
[frontmatter utility][frontmatter].

Replace uses Go `regexp` when regex is enabled, not PCRE. Defaults are first
match, literal search, and success on no match. With regex and `all: false`, the
replacement is spliced literally; with `all: true`, `ReplaceAllString` expands
capture references such as `${1}` (quote them against shell expansion).
Literal-search replacement stays literal even with all enabled. `matchCount`
counts all matches found, not replacements performed; a successful no-change
result need not write. Missing required matches yield `442`; invalid patterns
return `443`. See [ReplaceContent][note-service].

### Recycle clearing is not physical erasure

Soft deletion moves a note into the recycle bin; restore reads the recycled
record. Recycle clearing updates `rename` to **2** on deleted records whose
prior rename marker was 0, and updates their timestamps. It does not physically
delete their records or guarantee secure erasure of data. They cease appearing
in ordinary recycle queries. The raw single-note clear can succeed even when
no matching recycled record is changed; success is not proof of erasure.

The [service][note-service] derives `pathHash` from a supplied path; full clearing
must leave **both path and pathHash empty**. The [repository][note-repository]
filters by a nonempty hash, otherwise affects the vault's ordinary recycled
records. The script's explicit `--all` plus exact-vault confirmation is therefore
a required local safety boundary, not a server-side confirmation or transaction.

### Links and files do not imply an attachment lifecycle

Read returns `content`, `path`, `pathHash`, `contentHash`, `ctime`, `mtime`,
`version`, and `fileLinks` alongside other metadata. `fileLinks` maps embedded
links to resolved file locations. It is response metadata only; note writes,
renames, deletion, restoration, and recycle clearing do not promise the
corresponding attachment lifecycle. History, backlinks/outlinks, and attachment
CLI operations are out of scope. See [GET implementation][note-handler] and
[response DTOs][note-dto].

## Verification

From the repository root:

```bash
bash -n obsidian-fastnotesync-skill/scripts/fns.sh
shellcheck obsidian-fastnotesync-skill/scripts/fns.sh
python3 -B -m unittest discover -s obsidian-fastnotesync-skill/tests -v
if command -v skills-ref >/dev/null 2>&1; then
  skills-ref validate obsidian-fastnotesync-skill
fi
```

Runtime requires Bash 3.2+, curl, jq, and standard Unix `iconv` for UTF-8
validation, plus standard utilities such as `cp`, `cat`, `mktemp`, `mv`, and `rm`.
Tests require Python 3.8+ and use only its standard library; Python is not a
runtime dependency. The `-B` test invocation prevents Python bytecode cache
creation. Use offline/mock tests for parser, UTF-8 validation, proxy routing,
response, secret-redaction, transport, and precondition behavior. They do not prove
real-server concurrency guarantees or live-server compatibility. Report
documentation, syntax/lint, unit-test, and live-test results separately.

### Opt-in live runner

**Live write tests require explicit authorization and an isolated test vault.**
Prerequisites: `FNS_AUTH_MODE=token`, securely provisioned `FNS_BASE_URL` /
`FNS_TOKEN`, and an existing dedicated `FNS_VAULT` named `fns-skill-test-*`,
created outside this CLI. Prefer a token scoped to that vault. Manually opt in:

```bash
python3 -B obsidian-fastnotesync-skill/tests/run_live.py --confirm-vault "$FNS_VAULT"
```

Optional `--allow-clear` additionally opts into authorized recycle-clear tests.
Fixture operations use current-run UUID path guards. Active fixtures and the
vault are retained; the runner is not full teardown. Full clearing requires an
initially empty vault and verification that every recycled path is within the
current run's UUID scope. These checks are **not atomic**; ensure no concurrent
writers throughout the run. Confirmation flags do not authorize other vaults.
Never include private instance URLs, actual vault names, credentials, live note
data, or run-specific results in this documentation.

## Official sources at the frozen revision

[release]: https://github.com/haierkeys/fast-note-sync-service/releases/tag/3.6.1
[release-api]: https://api.github.com/repos/haierkeys/fast-note-sync-service/releases/tags/3.6.1
[routes]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/routers/router_api.go
[login-handler]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/routers/api_router/handler_user.go#L75-L116
[login-dto]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/dto/user_dto.go#L25-L31
[auth]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/middleware/user_auth_token.go
[webgui]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/middleware/webgui_auth.go
[note-dto]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/dto/note_dto.go
[note-handler]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/routers/api_router/handler_note.go
[note-service]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/service/note_service.go
[note-repository]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/dao/note_repository.go
[vault-service]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/internal/service/vault_service.go#L180-L206
[frontmatter]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/util/frontmatter.go
[response]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/app/app.go
[errors]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/errors/errors.go
[codes]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/code/common.go
[code-implementation]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/code/code.go#L323-L325
[pagination]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/app/pagination.go
[path-validation]: https://github.com/haierkeys/fast-note-sync-service/blob/7a6c78792c631f999c8a5f725bba5dd7235d6688/pkg/util/path.go
