#!/usr/bin/env bash
# Fast Note Sync REST client. Compatible with macOS Bash 3.2.
set +x
set -euo pipefail
umask 077

WORK=""
TOKEN=""
RESULT=""
COMMAND=""
SEEN=" "
VAULT=${FNS_VAULT:-}
PATH_VALUE="" PATH_HASH="" OLD_PATH="" OLD_PATH_HASH=""
CONTENT="" CONTENT_FILE="" CONTENT_SOURCE=""
CTIME="" MTIME="" EXPECT_HASH=""
KEYWORD="" SEARCH_MODE="" PAGE=1 PAGE_SIZE=10 SORT_BY=mtime SORT_ORDER=desc
RECYCLE=false REGEX=false ALL=false FAIL_NO_MATCH=false CONFIRM=false
CONFIRM_VAULT="" FIND="" REPLACE="" UPDATES="" REMOVE=""

cleanup() { if [[ -n "$WORK" ]]; then rm -rf -- "$WORK"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Use generic diagnostics: upstream messages can echo credentials or note bodies.
fail() {
  local exit_code=$1 type=$2 message=$3 code=${4:-} http=${5:-}
  jq -cn --arg type "$type" --arg message "$message" --arg code "$code" --arg http "$http" '
    {success:false,error:({type:$type,message:$message}
      + (if $code == "" then {} else {code:($code|tonumber)} end)
      + (if $http == "" then {} else {httpStatus:($http|tonumber)} end))}'
  exit "$exit_code"
}
usage_error() { fail 2 usage "$1"; }

usage() {
  printf '%s\n' \
    'Usage: fns.sh [--vault V] COMMAND [OPTIONS]' \
    'Environment: FNS_BASE_URL, FNS_TOKEN, FNS_VAULT; optional FNS_PROXY, FNS_CLIENT.' \
    'Explicit password mode: FNS_AUTH_MODE=password, FNS_USERNAME, FNS_PASSWORD.' \
    'Public: doctor | health | version. Authenticated discovery: vaults.' \
    'list/search [--keyword K --search-mode path|content --page N --page-size 1..100]' \
    '            [--sort-by mtime|ctime|path --sort-order asc|desc --recycle]' \
    'read/delete/restore --path P [--path-hash H] (read also accepts --recycle)' \
    'create/update/upsert --path P CONTENT_SOURCE [--ctime MS --mtime MS --path-hash H]' \
    'update also accepts --expect-hash H (precheck only, NOT atomic CAS).' \
    'append/prepend --path P CONTENT_SOURCE [--path-hash H]' \
    'CONTENT_SOURCE: exactly one of --content TEXT, --content-file FILE, --stdin.' \
    'replace --path P --find STR --replace STR [--regex --all --fail-if-no-match]' \
    'frontmatter --path P [--updates JSON_OBJECT --remove JSON_STRING_ARRAY]' \
    'rename --old-path P --path Q [--old-path-hash H --path-hash H]' \
    'recycle-clear --path P --confirm [--path-hash H]' \
    'recycle-clear --all --confirm-vault EXACT_VAULT' \
    '--vault overrides FNS_VAULT anywhere. Secrets must never be CLI arguments.'
}

positive_integer() { [[ "$1" =~ ^[1-9][0-9]*$ && ${#1} -le 9 ]]; }
timestamp() { [[ "$1" =~ ^(0|[1-9][0-9]*)$ && ${#1} -le 16 ]]; }
no_newline() { [[ "$1" != *$'\n'* && "$1" != *$'\r'* ]]; }

parse() {
  local opt value allowed
  while [[ $# -gt 0 ]]; do
    opt=$1
    if [[ "$opt" == --help || "$opt" == -h ]]; then usage; exit 0; fi
    if [[ "$opt" != --* ]]; then
      [[ -z "$COMMAND" ]] || usage_error 'Unexpected positional argument'
      case "$opt" in
        doctor|health|version|vaults|list|search|read|create|update|upsert|delete|restore|append|prepend|replace|frontmatter|rename|recycle-clear) COMMAND=$opt ;;
        *) usage_error 'Unknown command' ;;
      esac
      shift
      continue
    fi
    [[ "$SEEN" != *" $opt "* ]] || usage_error 'Duplicate option'
    SEEN="$SEEN$opt "
    allowed=false
    case "$opt:$COMMAND" in
      --vault:*) allowed=true ;;
      --path:read|--path:delete|--path:restore|--path:create|--path:update|--path:upsert|--path:append|--path:prepend|--path:replace|--path:frontmatter|--path:rename|--path:recycle-clear) allowed=true ;;
      --path-hash:read|--path-hash:delete|--path-hash:restore|--path-hash:create|--path-hash:update|--path-hash:upsert|--path-hash:append|--path-hash:prepend|--path-hash:replace|--path-hash:frontmatter|--path-hash:rename|--path-hash:recycle-clear) allowed=true ;;
      --content:create|--content:update|--content:upsert|--content:append|--content:prepend|--content-file:create|--content-file:update|--content-file:upsert|--content-file:append|--content-file:prepend|--stdin:create|--stdin:update|--stdin:upsert|--stdin:append|--stdin:prepend) allowed=true ;;
      --ctime:create|--ctime:update|--ctime:upsert|--mtime:create|--mtime:update|--mtime:upsert|--expect-hash:update) allowed=true ;;
      --keyword:list|--keyword:search|--search-mode:list|--search-mode:search|--page:list|--page:search|--page-size:list|--page-size:search|--sort-by:list|--sort-by:search|--sort-order:list|--sort-order:search|--recycle:list|--recycle:search|--recycle:read) allowed=true ;;
      --find:replace|--replace:replace|--regex:replace|--all:replace|--fail-if-no-match:replace|--updates:frontmatter|--remove:frontmatter|--old-path:rename|--old-path-hash:rename|--all:recycle-clear|--confirm:recycle-clear|--confirm-vault:recycle-clear) allowed=true ;;
    esac
    [[ "$allowed" == true ]] || usage_error 'Unknown or inapplicable option'
    case "$opt" in
      --stdin|--recycle|--regex|--all|--fail-if-no-match|--confirm) value=true; shift ;;
      --content|--find|--replace|--keyword)
        [[ $# -ge 2 ]] || usage_error 'Option requires a value'
        value=$2; shift 2 ;;
      *)
        [[ $# -ge 2 && "$2" != --* ]] || usage_error 'Option requires a value'
        value=$2; shift 2 ;;
    esac
    case "$opt" in
      --vault) VAULT=$value ;;
      --path) PATH_VALUE=$value ;;
      --path-hash) PATH_HASH=$value ;;
      --old-path) OLD_PATH=$value ;;
      --old-path-hash) OLD_PATH_HASH=$value ;;
      --content|--content-file|--stdin)
        [[ -z "$CONTENT_SOURCE" ]] || usage_error 'Choose exactly one content source'
        CONTENT_SOURCE=$opt
        case "$opt" in --content) CONTENT=$value ;; --content-file) CONTENT_FILE=$value ;; esac ;;
      --ctime) CTIME=$value; timestamp "$value" || usage_error 'Invalid ctime' ;;
      --mtime) MTIME=$value; timestamp "$value" || usage_error 'Invalid mtime' ;;
      --expect-hash) EXPECT_HASH=$value; [[ -n "$value" ]] || usage_error 'Expected hash cannot be empty' ;;
      --keyword) KEYWORD=$value ;;
      --search-mode) SEARCH_MODE=$value; [[ "$value" == path || "$value" == content ]] || usage_error 'Unsupported search mode' ;;
      --page) PAGE=$value; positive_integer "$value" || usage_error 'Invalid page' ;;
      --page-size) PAGE_SIZE=$value; positive_integer "$value" && [[ "$value" -le 100 ]] || usage_error 'Page size must be 1..100' ;;
      --sort-by) SORT_BY=$value; [[ "$value" == mtime || "$value" == ctime || "$value" == path ]] || usage_error 'Unsupported sort field' ;;
      --sort-order) SORT_ORDER=$value; [[ "$value" == asc || "$value" == desc ]] || usage_error 'Unsupported sort order' ;;
      --recycle) RECYCLE=true ;;
      --regex) REGEX=true ;;
      --all) ALL=true ;;
      --fail-if-no-match) FAIL_NO_MATCH=true ;;
      --confirm) CONFIRM=true ;;
      --confirm-vault) CONFIRM_VAULT=$value ;;
      --find) FIND=$value ;;
      --replace) REPLACE=$value ;;
      --updates) UPDATES=$value ;;
      --remove) REMOVE=$value ;;
    esac
  done
  [[ -n "$COMMAND" ]] || usage_error 'Command required'
  case "$COMMAND" in
    doctor|health|version|vaults|list|search) ;;
    recycle-clear)
      if [[ "$ALL" == true ]]; then
        [[ -n "$VAULT" && "$CONFIRM_VAULT" == "$VAULT" && "$CONFIRM" == false && "$SEEN" != *' --path '* && "$SEEN" != *' --path-hash '* ]] || usage_error 'Full clear requires only --all and an exact --confirm-vault'
      else
        [[ -n "$PATH_VALUE" && "$CONFIRM" == true && "$SEEN" != *' --confirm-vault '* ]] || usage_error 'Single clear requires --path and --confirm'
      fi ;;
    *) [[ -n "$PATH_VALUE" ]] || usage_error 'Nonempty --path required' ;;
  esac
  case "$COMMAND" in
    create|update|upsert|append|prepend)
      [[ -n "$CONTENT_SOURCE" ]] || usage_error 'Explicit content source required'
      if [[ "$CONTENT_SOURCE" == --content-file ]]; then
        [[ -f "$CONTENT_FILE" && -r "$CONTENT_FILE" ]] || usage_error 'Content file must be a readable regular file'
      fi ;;
    replace) [[ -n "$FIND" && "$SEEN" == *' --replace '* ]] || usage_error 'Nonempty --find and explicit --replace required' ;;
    rename) [[ -n "$OLD_PATH" ]] || usage_error 'Nonempty --old-path required' ;;
    frontmatter)
      [[ "$SEEN" == *' --updates '* || "$SEEN" == *' --remove '* ]] || usage_error 'Frontmatter requires updates or remove'
      if [[ "$SEEN" == *' --updates '* ]]; then
        printf '%s' "$UPDATES" | jq -se 'length == 1 and (.[0]|type == "object")' >/dev/null 2>&1 || usage_error 'Updates must be one JSON object'
      fi
      if [[ "$SEEN" == *' --remove '* ]]; then
        printf '%s' "$REMOVE" | jq -se 'length == 1 and (.[0]|type == "array" and all(.[];type == "string"))' >/dev/null 2>&1 || usage_error 'Remove must be one JSON string array'
      fi ;;
  esac
  if [[ "$COMMAND" == search ]]; then
    [[ -n "$KEYWORD" ]] || usage_error 'Search requires a nonempty keyword'
    SEARCH_MODE=${SEARCH_MODE:-content}
  else SEARCH_MODE=${SEARCH_MODE:-path}; fi
}

configure() {
  BASE=${FNS_BASE_URL:-}
  BASE=${BASE%/}
  CLIENT=${FNS_CLIENT:-obsidian-fastnotesync-skill}
  AGENT=${FNS_USER_AGENT:-obsidian-fastnotesync-skill/2.0.0}
  MODE=${FNS_AUTH_MODE:-token}
  TIMEOUT=${FNS_TIMEOUT:-30}
  CONNECT_TIMEOUT=${FNS_CONNECT_TIMEOUT:-10}
  local value authority host
  for value in "$BASE" "$CLIENT" "$AGENT" "${FNS_TOKEN:-}" "${FNS_PROXY:-}"; do
    no_newline "$value" || fail 2 config 'Newlines are not allowed in transport configuration'
  done
  [[ "$BASE" =~ ^https?://[^/]+(/[^\?\#]*)?$ && "$BASE" != *'?'* && "$BASE" != *'#'* && "$BASE" != *' '* && "$BASE" != *'"'* && "$BASE" != *\\* ]] || fail 2 config 'Invalid server base URL'
  authority=${BASE#*://}; authority=${authority%%/*}
  [[ "$authority" != *@* ]] || fail 2 config 'Credentials must not be embedded in the server URL'
  [[ "$authority" =~ ^([[:alnum:]_.-]+(:[0-9]+)?|\[[0-9a-fA-F:]+\](:[0-9]+)?)$ && "$BASE" != *'{'* && "$BASE" != *'}'* ]] || fail 2 config 'Base URL must address one server'
  host=${authority%%:*}
  if [[ "$BASE" == http://* && "$host" != localhost && "$host" != 127.0.0.1 && "$authority" != '[::1]' && "$authority" != '[::1]:'* && "${FNS_ALLOW_HTTP:-}" != 1 ]]; then
    fail 2 config 'Non-loopback HTTP requires FNS_ALLOW_HTTP=1'
  fi
  if ! positive_integer "$TIMEOUT" || ! positive_integer "$CONNECT_TIMEOUT"; then
    fail 2 config 'Timeouts must be positive integer seconds'
  fi
  [[ "$MODE" == token || "$MODE" == password ]] || fail 2 config 'Unsupported authentication mode'
  if [[ -n "${FNS_PROXY:-}" ]]; then
    [[ "$FNS_PROXY" =~ ^(socks5h|socks5|http|https)://[^/]+/?$ && "$FNS_PROXY" != *' '* ]] || fail 2 config 'Invalid proxy URL'
  fi
  case "$COMMAND" in
    doctor|health|version) ;;
    *)
      if [[ "$MODE" == token ]]; then
        [[ -n "${FNS_TOKEN:-}" ]] || fail 2 config 'FNS_TOKEN required in token mode'
        TOKEN=$FNS_TOKEN
      else
        [[ -n "${FNS_USERNAME:-}" && -n "${FNS_PASSWORD:-}" ]] || fail 2 config 'Username and password required in explicit password mode'
        CLIENT=webgui
      fi
      [[ "$COMMAND" == vaults || -n "$VAULT" ]] || fail 2 config 'An existing vault must be selected' ;;
  esac
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/fns.XXXXXXXX") || fail 2 config 'Unable to create private temporary directory'
}

# Escape curl config strings; values must never become command-line arguments.
config_line() {
  local value=$2
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s = "%s"\n' "$1" "$value"
}

request() {
  local method=$1 endpoint=$2 auth=$3 body=${4:-} query=${5:-} url http rc code
  url="$BASE$endpoint"
  [[ -z "$query" ]] || url="$url?$query"
  {
    config_line url "$url"
    config_line request "$method"
    config_line user-agent "$AGENT"
    config_line max-time "$TIMEOUT"
    config_line connect-timeout "$CONNECT_TIMEOUT"
    config_line proto '=http,https'
    printf '%s\n' 'globoff'
    config_line header "x-client: $CLIENT"
    if [[ "$auth" == true ]]; then config_line header "Authorization: Bearer $TOKEN"; fi
    if [[ -n "${FNS_PROXY:-}" ]]; then
      config_line proxy "$FNS_PROXY"
      # An explicit skill proxy takes precedence over inherited NO_PROXY.
      config_line noproxy ''
    fi
    if [[ -n "$body" ]]; then
      config_line header 'Content-Type: application/json'
      config_line data-binary "@$body"
    fi
  } > "$WORK/curl.conf"
  rc=0
  http=$(curl -q --config "$WORK/curl.conf" --silent --show-error --output "$WORK/response.json" --write-out '%{http_code}' 2> "$WORK/curl.err") || rc=$?
  [[ "$rc" == 0 ]] || fail 3 transport 'Request failed; write outcome may be unknown. Do not automatically retry.'
  [[ "$http" =~ ^[0-9]{3}$ ]] || fail 4 protocol 'Invalid HTTP status from transport'
  [[ "$http" == 2?? ]] || fail 3 http 'Unsuccessful HTTP response; redirects are not followed' '' "$http"
  jq -se 'length == 1 and (.[0]|type == "object" and (.code|type == "number") and (.code == (.code|floor)) and ((has("status")|not) or (.status|type == "boolean")))' "$WORK/response.json" >/dev/null 2>&1 || fail 4 protocol 'Invalid JSON response envelope' '' "$http"
  code=$(jq -r '.code' "$WORK/response.json")
  case "$code" in
    1|2|3|4|5|6)
      if jq -e '.status == false' "$WORK/response.json" >/dev/null; then fail 5 business 'Server reported a business failure' "$code" "$http"; fi ;;
    0|30[0-9]|31[0-5]|40[0-9]|41[0-4]|42[0-3]|43[0-9]|44[0-9]|45[0-9]|46[0-7]|47[0-9]|48[0-4]|49[1-9]|50[0-2]|51[0-2]|52[01]|530)
      fail 5 business 'Server rejected the operation; consult the API error code' "$code" "$http" ;;
    *) fail 4 protocol 'Unrecognized business code; server contract may be incompatible' "$code" "$http" ;;
  esac
  RESULT=$(jq -c '{success:true,code,data:(if has("data") then .data else null end)}' "$WORK/response.json")
}

percent_decode() {
  local value=$1 decoded='' byte prefix hex suffix
  while [[ "$value" =~ ^([^%]*)%([0-9a-fA-F]{2})(.*)$ ]]; do
    prefix=${BASH_REMATCH[1]}; hex=${BASH_REMATCH[2]}; suffix=${BASH_REMATCH[3]}
    printf -v byte '%b' "\\x$hex"
    decoded="$decoded$prefix$byte"
    value=$suffix
  done
  printf '%s' "$decoded$value"
}

print_result() {
  local authority credentials proxy=${FNS_PROXY:-}
  export FNS_CURRENT_TOKEN="$TOKEN"
  export FNS_PROXY_USERNAME='' FNS_PROXY_PASSWORD=''
  export FNS_PROXY_DECODED_USERNAME='' FNS_PROXY_DECODED_PASSWORD=''
  authority=${proxy#*://}; authority=${authority%%/*}
  if [[ "$authority" == *@* ]]; then
    credentials=${authority%@*}
    FNS_PROXY_USERNAME=${credentials%%:*}
    if [[ "$credentials" == *:* ]]; then FNS_PROXY_PASSWORD=${credentials#*:}; fi
    FNS_PROXY_DECODED_USERNAME=$(percent_decode "$FNS_PROXY_USERNAME")
    FNS_PROXY_DECODED_PASSWORD=$(percent_decode "$FNS_PROXY_PASSWORD")
  fi
  printf '%s\n' "$RESULT" | jq -c '
    ([env.FNS_TOKEN,env.FNS_CURRENT_TOKEN,env.FNS_USERNAME,env.FNS_PASSWORD,env.FNS_PROXY,
      env.FNS_PROXY_USERNAME,env.FNS_PROXY_PASSWORD,env.FNS_PROXY_DECODED_USERNAME,env.FNS_PROXY_DECODED_PASSWORD]
      | map(select(type == "string" and length > 0)) | unique | sort_by(-length)) as $secrets
    | def redact:
        if type == "string" then reduce $secrets[] as $s (. ; split($s)|join("[REDACTED]"))
        elif type == "array" then map(redact)
        elif type == "object" then with_entries(.key |= redact | .value |= redact)
        else . end;
      .data |= redact'
}

query_note() {
  jq -rn --arg vault "$VAULT" --arg path "$PATH_VALUE" --arg hash "$PATH_HASH" --arg recycle "$RECYCLE" --arg cmd "$COMMAND" '
    {vault:$vault,path:$path}
    + (if $hash == "" then {} else {pathHash:$hash} end)
    + (if $cmd == "read" then {isRecycle:$recycle} else {} end)
    | to_entries | map((.key|@uri)+"="+(.value|@uri)) | join("&")'
}

note_body() {
  jq -cn --arg vault "$VAULT" --arg path "$PATH_VALUE" --arg hash "$PATH_HASH" '
    {vault:$vault,path:$path} + (if $hash == "" then {} else {pathHash:$hash} end)' > "$WORK/body.json"
}

prepare_content() {
  case "$CONTENT_SOURCE" in
    --content) printf '%s' "$CONTENT" > "$WORK/content.raw" ;;
    --content-file) cp -- "$CONTENT_FILE" "$WORK/content.raw" ;;
    --stdin) cat > "$WORK/content.raw" ;;
    *) return ;;
  esac
  # jq may replace invalid UTF-8; reject decoding loss before login or writes.
  iconv -f UTF-8 -t UTF-8 < "$WORK/content.raw" > /dev/null 2>&1 || usage_error 'Note content must be valid UTF-8'
  jq -Rs . < "$WORK/content.raw" > "$WORK/content.json"
  # Some iconv implementations accept out-of-range Unicode scalars. Verify that
  # jq's JSON serialization did not replace bytes or strip content as well.
  jq -jr . "$WORK/content.json" > "$WORK/roundtrip.raw"
  cmp -s "$WORK/content.raw" "$WORK/roundtrip.raw" || usage_error 'Content could not be encoded losslessly as UTF-8'
}

content_body() {
  jq -c --slurpfile content "$WORK/content.json" '. + {content:$content[0]}' "$WORK/body.json" > "$WORK/next.json"
  mv "$WORK/next.json" "$WORK/body.json"
}

main() {
  command -v jq >/dev/null 2>&1 || { printf '%s\n' '{"success":false,"error":{"type":"config","message":"jq required"}}'; exit 2; }
  parse "$@"
  command -v curl >/dev/null 2>&1 || fail 2 config 'curl required'
  configure
  if [[ -n "$CONTENT_SOURCE" ]]; then
    command -v iconv >/dev/null 2>&1 || fail 2 config 'iconv required to validate UTF-8 content'
    prepare_content
  fi
  case "$COMMAND" in
    doctor)
      request GET /api/health false
      printf '%s\n' "$RESULT" > "$WORK/health.json"
      request GET /api/version false
      printf '%s\n' "$RESULT" > "$WORK/version.json"
      RESULT=$(jq -cn --slurpfile health "$WORK/health.json" --slurpfile version "$WORK/version.json" '{success:true,code:1,data:{health:$health[0].data,version:$version[0].data}}')
      print_result
      return ;;
    health|version) request GET "/api/$COMMAND" false; print_result; return ;;
  esac
  if [[ "$MODE" == password ]]; then
    # jq reads environment secrets; neither jq nor curl argv contain credentials.
    jq -cn '{credentials:env.FNS_USERNAME,password:env.FNS_PASSWORD}' > "$WORK/login.json"
    request POST /api/user/login false "$WORK/login.json"
    TOKEN=$(jq -er '.data.token | select(type == "string" and length > 0)' "$WORK/response.json") || fail 4 protocol 'Login did not return a nonempty token'
    no_newline "$TOKEN" || fail 4 protocol 'Login returned an invalid token'
  fi
  local query ctime_value
  case "$COMMAND" in
    vaults) request GET /api/vault true ;;
    list|search)
      query=$(jq -rn --arg vault "$VAULT" --arg keyword "$KEYWORD" --arg mode "$SEARCH_MODE" --arg page "$PAGE" --arg size "$PAGE_SIZE" --arg sort "$SORT_BY" --arg order "$SORT_ORDER" --arg recycle "$RECYCLE" '
        {vault:$vault,keyword:$keyword,searchMode:$mode,page:$page,pageSize:$size,sortBy:$sort,sortOrder:$order,isRecycle:$recycle}
        | to_entries | map((.key|@uri)+"="+(.value|@uri)) | join("&")')
      request GET /api/notes true '' "$query"
      # Go nil slices serialize as null for empty results; expose one list shape.
      printf '%s\n' "$RESULT" | jq -e '.data | type == "object" and has("list") and (.list | type == "array" or type == "null")' >/dev/null || fail 4 protocol 'Invalid note list response'
      if printf '%s\n' "$RESULT" | jq -e '.data.list == null' >/dev/null; then
        printf '%s\n' "$RESULT" | jq -e '.data.pager.totalRows == 0' >/dev/null || fail 4 protocol 'Null note list with nonzero or missing count'
        RESULT=$(printf '%s\n' "$RESULT" | jq -c '.data.list = []')
      fi ;;
    read|delete)
      query=$(query_note)
      if [[ "$COMMAND" == read ]]; then request GET /api/note true '' "$query";
      else request DELETE /api/note true '' "$query"; fi ;;
    create|upsert|update|append|prepend)
      note_body
      content_body
      if [[ "$COMMAND" == update ]]; then
        query=$(query_note)
        request GET /api/note true '' "$query"
        jq -e '.data|type == "object" and (.ctime|type == "number" and . >= 0 and . == floor)' "$WORK/response.json" >/dev/null || fail 4 protocol 'Existing note has invalid creation metadata'
        if [[ -n "$EXPECT_HASH" ]]; then
          ctime_value=$(jq -r '.data.contentHash // ""' "$WORK/response.json")
          [[ "$ctime_value" == "$EXPECT_HASH" ]] || fail 6 precondition 'Content hash changed or is missing; no write performed'
        fi
        if [[ "$SEEN" != *' --ctime '* ]]; then CTIME=$(jq -r '.data.ctime' "$WORK/response.json"); fi
      fi
      case "$COMMAND" in
        create|upsert|update)
          jq -c --arg ctime "$CTIME" --arg mtime "$MTIME" --arg cmd "$COMMAND" '
            . + {createOnly:($cmd == "create")}
            + (if $ctime == "" then {} else {ctime:($ctime|tonumber)} end)
            + (if $mtime == "" then {} else {mtime:($mtime|tonumber)} end)' "$WORK/body.json" > "$WORK/next.json"
          mv "$WORK/next.json" "$WORK/body.json"
          request POST /api/note true "$WORK/body.json" ;;
        *) request POST "/api/note/$COMMAND" true "$WORK/body.json" ;;
      esac ;;
    replace)
      note_body
      jq -c --arg find "$FIND" --arg replace "$REPLACE" --argjson regex "$REGEX" --argjson all "$ALL" --argjson fail "$FAIL_NO_MATCH" '. + {find:$find,replace:$replace,regex:$regex,all:$all,failIfNoMatch:$fail}' "$WORK/body.json" > "$WORK/next.json"
      request POST /api/note/replace true "$WORK/next.json" ;;
    frontmatter)
      note_body
      if [[ "$SEEN" == *' --updates '* ]]; then
        printf '%s' "$UPDATES" > "$WORK/updates.json"
        jq -c --slurpfile updates "$WORK/updates.json" '. + {updates:$updates[0]}' "$WORK/body.json" > "$WORK/next.json"
        mv "$WORK/next.json" "$WORK/body.json"
      fi
      if [[ "$SEEN" == *' --remove '* ]]; then
        printf '%s' "$REMOVE" > "$WORK/remove.json"
        jq -c --slurpfile remove "$WORK/remove.json" '. + {remove:$remove[0]}' "$WORK/body.json" > "$WORK/next.json"
        mv "$WORK/next.json" "$WORK/body.json"
      fi
      request PATCH /api/note/frontmatter true "$WORK/body.json" ;;
    rename)
      note_body
      jq -c --arg old "$OLD_PATH" --arg hash "$OLD_PATH_HASH" '. + {oldPath:$old} + (if $hash == "" then {} else {oldPathHash:$hash} end)' "$WORK/body.json" > "$WORK/next.json"
      request POST /api/note/rename true "$WORK/next.json" ;;
    restore) note_body; request PUT /api/note/restore true "$WORK/body.json" ;;
    recycle-clear)
      if [[ "$ALL" == true ]]; then
        jq -cn --arg vault "$VAULT" '{vault:$vault}' > "$WORK/body.json"
      else note_body; fi
      request DELETE /api/note/recycle-clear true "$WORK/body.json" ;;
  esac
  print_result
}

main "$@"
