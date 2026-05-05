#!/usr/bin/env bash
# bw.sh — Bitwarden/Vaultwarden CLI Wrapper
#   bw.sh -e user@example.com -p <pwd> 'bw://"Item"/password'
#   bw.sh -p <pwd> list --search "github"
#   bw.sh -p <pwd> get <name> --field password
#   bw.sh -p <pwd> create '{"type":1,...}'
#   bw.sh -p <pwd> edit <id> '.name="New"'
#   bw.sh -p <pwd> delete <id>
#   bw.sh -p <pwd> folders
set -euo pipefail

PASSWORD="" BW_EMAIL="" PROXY="" DO_SYNC=false NO_UNLOCK=false
SUBCOMMAND="" SUB_ARGS=()

json_error()   { printf '{"success":false,"error":"%s"}\n' "$1"; exit 1; }
json_success() { printf '{"success":true,"data":%s}\n' "$1"; }

setup_proxy() {
  if [ -n "${PROXY:-}" ]; then
    export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" https_proxy="$PROXY" http_proxy="$PROXY"
  fi
}

ensure_session() {
  setup_proxy
  [ "$NO_UNLOCK" = true ] && return 0
  local s; s=$(bw status 2>/dev/null | jq -r '.status // "unauthenticated"')
  case "$s" in
    unlocked) return 0 ;;
    locked)
      [ -z "${BW_PASSWORD:-}" ] && json_error "password required (-p or BW_PASSWORD)"
      export BW_SESSION=$(bw unlock --passwordenv BW_PASSWORD --raw 2>/dev/null) || true
      [ -n "${BW_SESSION:-}" ] || json_error "unlock failed — check password"
      ;;
    *)
      [ -z "${BW_EMAIL:-}" ]   && json_error "email required (-e or BW_EMAIL)"
      [ -z "${BW_PASSWORD:-}" ] && json_error "password required (-p or BW_PASSWORD)"
      bw login "$BW_EMAIL" --passwordenv BW_PASSWORD >/dev/null 2>&1 \
        || json_error "login failed — check email/password/server URL"
      export BW_SESSION=$(bw unlock --passwordenv BW_PASSWORD --raw 2>/dev/null) || true
      [ -n "${BW_SESSION:-}" ] || json_error "unlock failed after login"
      ;;
  esac
}

# ─── resolve ──────────────────────────────────────────────────────────
do_resolve() {
  [ "$DO_SYNC" = true ] && bw sync >/dev/null 2>&1
  local targets=$(
    printf '%s\n' "$@" \
    | awk -F'"' '{print $2"|"substr($3,2)}' \
    | jq -R 'split("|") | {name:.[0],key:.[1]}' | jq -s '.'
  )
  local result=$(bw list items | jq --argjson targets "$targets" '
    . as $items | reduce $targets[] as $t ({};
      ($items | map(select(.name == $t.name))[0]) as $item
      | (if ($t.key | startswith("fields/")) then
           ($item.fields // []) | map(select(.name == ($t.key | ltrimstr("fields/"))))[0].value
           | if . then {($t.key | ltrimstr("fields/")): .} else {($t.key | ltrimstr("fields/")): "NOT FOUND"} end
         elif $t.key == "notes" then
           {($t.key): ($item.notes // "NOT FOUND")}
         elif $t.key == "uri" then
           {($t.key): (($item.login.uris // [])[0].uri // "NOT FOUND")}
         else
           {($t.key): ($item.login[$t.key] // "NOT FOUND")}
         end) as $entry
      | if .[$t.name] then .[$t.name] += $entry else .[$t.name] = $entry end
    )
  ')
  json_success "$result"
}

# ─── list ─────────────────────────────────────────────────────────────
do_list() {
  [ "$DO_SYNC" = true ] && bw sync >/dev/null 2>&1
  local bw_args=("list" "items") jq_filter="."
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --search) bw_args+=("--search" "$2"); shift 2 ;;
      --folder) bw_args+=("--folderid" "$2"); shift 2 ;;
      --type)   jq_filter="map(select(.type == $2))"; shift 2 ;;
      --trash)  bw_args+=("--trash"); shift ;;
      *) shift ;;
    esac
  done
  local result=$(bw "${bw_args[@]}" | jq "[.[] | {id,name,type,folderId,username:(.login.username//\"-\")}] | $jq_filter")
  json_success "$result"
}

# ─── get ──────────────────────────────────────────────────────────────
do_get() {
  [ "$DO_SYNC" = true ] && bw sync >/dev/null 2>&1
  local target="" field=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --field) field="$2"; shift 2 ;;
      *) target="$1"; shift ;;
    esac
  done
  [ -z "$target" ] && json_error "get requires an item id or name"

  local item
  item=$(bw get item "$target" 2>/dev/null | jq -c '.') || {
    item=$(bw list items --search "$target" | jq -c --arg n "$target" 'map(select(.name==$n))[0]')
    [ "$item" = "null" ] && json_error "item not found: $target"
  }

  local item_id=$(echo "$item" | jq -r '.id')
  [ -z "$field" ] && { json_success "$item"; return; }

  case "$field" in
    password|username|totp) json_success "$(bw get "$field" "$item_id" | jq -R '.')" ;;
    notes) json_success "$(echo "$item" | jq '{notes:(.notes//"NOT FOUND")}')" ;;
    uri)   json_success "$(echo "$item" | jq '{uri:((.login.uris//[])[0].uri//"NOT FOUND")}')" ;;
    *)     json_success "$item" ;;
  esac
}

# ─── create ───────────────────────────────────────────────────────────
do_create() {
  local json_input="${1:-}"; [ -z "$json_input" ] && json_error "create requires JSON"
  local json_content
  if [ "$json_input" = "-" ]; then json_content=$(cat)
  elif [ -f "$json_input" ]; then json_content=$(cat "$json_input")
  else json_content="$json_input"
  fi
  echo "$json_content" | jq empty 2>/dev/null || json_error "invalid JSON"
  local result=$(echo "$json_content" | base64 | bw create item 2>/dev/null)
  json_success "$(echo "$result" | jq '{id,name,type,folderId}')"
}

# ─── edit ─────────────────────────────────────────────────────────────
do_edit() {
  local item_id="${1:-}" jq_patch="${2:-}"
  [ -z "$item_id" ] && json_error "edit requires item id"
  [ -z "$jq_patch" ] && json_error "edit requires jq patch expression"
  local result=$(bw get item "$item_id" 2>/dev/null | jq "$jq_patch" | base64 | bw edit item "$item_id" 2>/dev/null)
  json_success "$(echo "$result" | jq '{id,name,revisionDate}')"
}

# ─── delete ───────────────────────────────────────────────────────────
do_delete() {
  local target="${1:-}"; [ -z "$target" ] && json_error "delete requires item id or name"
  local item_id
  item_id=$(bw get item "$target" 2>/dev/null | jq -r '.id // empty') || true
  if [ -z "$item_id" ]; then
    item_id=$(bw list items --search "$target" | jq -r --arg n "$target" 'map(select(.name==$n))[0].id // empty')
  fi
  [ -z "$item_id" ] && json_error "item not found: $target"
  bw delete item "$item_id" >/dev/null 2>&1 || json_error "delete failed"
  json_success '{"deleted":true,"id":"'"$item_id"'"}'
}

# ─── folders ──────────────────────────────────────────────────────────
do_folders() {
  [ "$DO_SYNC" = true ] && bw sync >/dev/null 2>&1
  json_success "$(bw list folders | jq '[.[] | {id,name}]')"
}

do_lock() {
  bw lock >/dev/null 2>&1 || json_error "lock failed"
  json_success '"locked"'
}

do_logout() {
  bw logout >/dev/null 2>&1 || json_error "logout failed"
  json_success '"logged out"'
}

# ─── usage ────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
Usage: bw.sh [GLOBAL_OPTS] <SUBCOMMAND> [ARGS]

Global Options:
  -e, --email <email>     Login email (or set BW_EMAIL env var; required if not logged in)
  -p, --password <pwd>    Master password (or set BW_PASSWORD env var)
  --proxy <url>           Proxy for all bw network traffic (socks5:// socks5h:// http:// https://)
  -s, --sync              Sync vault before reading
  --no-unlock             Skip authentication (BW_SESSION already set)
  -h, --help              Show this help

Subcommands:
  resolve <uri>...       Batch resolve bw:// URIs (DEFAULT if no subcommand)
  list [--search <t>] [--type <n>] [--folder <id>] [--trash]
  get <id|name> [--field password|username|totp|notes|uri]
  create <json-string|file.json|->
  edit <id> '<jq-patch>'
  delete <id|name>
  folders
  lock                  Lock the vault
  logout                Log out (clears local credentials)

All output: {"success":true,"data":...} or {"success":false,"error":"..."}
EOF
}

# ─── main ─────────────────────────────────────────────────────────────
main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -e|--email)     BW_EMAIL="$2"; export BW_EMAIL="$2"; shift 2 ;;
      -p|--password) PASSWORD="$2"; export BW_PASSWORD="$2"; shift 2 ;;
      --proxy)       PROXY="$2"; shift 2 ;;
      -s|--sync)     DO_SYNC=true; shift ;;
      --no-unlock)   NO_UNLOCK=true; shift ;;
      -h|--help)     usage; exit 0 ;;
      *)             break ;;
    esac
  done

  if [[ "${1:-}" == bw://* ]]; then SUBCOMMAND="resolve"; SUB_ARGS=("$@")
  elif [[ -n "${1:-}" ]]; then SUBCOMMAND="$1"; shift; SUB_ARGS=("$@")
  else usage; exit 1
  fi

  # lock and logout don't need a session
  case "$SUBCOMMAND" in lock) do_lock; return ;; logout) do_logout; return ;; esac

  ensure_session

  case "$SUBCOMMAND" in
    resolve) do_resolve "${SUB_ARGS[@]}" ;;
    list)    do_list "${SUB_ARGS[@]}" ;;
    get)     do_get "${SUB_ARGS[@]}" ;;
    create)  do_create "${SUB_ARGS[@]}" ;;
    edit)    do_edit "${SUB_ARGS[@]}" ;;
    delete)  do_delete "${SUB_ARGS[@]}" ;;
    folders) do_folders ;;
    *)       json_error "unknown subcommand: $SUBCOMMAND" ;;
  esac
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
