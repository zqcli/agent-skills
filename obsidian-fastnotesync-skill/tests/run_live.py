#!/usr/bin/env python3
"""Explicitly authorized live contract checks; Python 3.10+ POSIX.

The runner uses the standard library; the Python CLI requires requests[socks]
and urllib3>=2.8.0,<3 for independent HTTPS proxy certificate verification.

Securely provision FNS_BASE_URL, FNS_VAULT, FNS_AUTH_MODE=token, and FNS_TOKEN
before invoking this runner. Strongly prefer a token scoped ONLY to the selected
existing fns-skill-test-* vault. The runner cannot verify token scope and never
provisions a vault or token. Do not use a shared vault or concurrent writers.

From the repository root:
    python3 -B obsidian-fastnotesync-skill/tests/run_live.py \\
        --confirm-vault "$FNS_VAULT"

Add --allow-clear only to authorize the optional single/full recycle-clear
checks. Full clear additionally requires an initially empty active/recycle vault
and a complete immediately preceding recycle scan containing only current-run
paths. That scan is NOT an atomic lock against concurrent writers.

Notes use fns-live-<32-hex-run-UUID>/<fixture-name>.md. Final active notes and the
vault remain in place. On failure, stop without automatic retries or note cleanup;
a timed-out write can have an uncertain outcome. Only content-search GETs poll
(up to five attempts). Reports never include credentials, URLs, note content,
raw child diagnostics, or arbitrary exception text.
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fns.py"
CALL_TIMEOUT = 120
SCAN_TIMEOUT = 120
MAX_SCAN_PAGES = 1000
SEARCH_ATTEMPTS = 5
SKIP_REASONS = {
    "not_authorized": "Recycle clearing was not authorized with --allow-clear.",
    "not_initially_empty": "Full recycle clear requires an initially empty active and recycle vault.",
    "search_not_ready": "Body-only content search was unavailable or indexing was not ready within five GET attempts; path-only fallback is possible.",
    "aborted": "Not run because an earlier check failed; no automatic cleanup was attempted.",
}
FAIL_REASON = "Operation or assertion failed; no automatic retry or note cleanup was attempted."
GUARD_REASON = "Refused configuration or missing prerequisites; require exact vault confirmation, an existing fns-skill-test-* vault, FNS_AUTH_MODE=token, FNS_TOKEN, and a valid FNS_BASE_URL. No live calls were made."


class TestFailure(Exception):
    """No raw diagnostics are attached to exceptions that reach the reporter."""


class SkippedCheck(Exception):
    def __init__(self, reason):
        self.reason = reason


def abort_test():
    raise TestFailure()


def require(condition):
    if not condition:
        abort_test()


def print_summary(results, prefix=None, tracked_paths=0):
    counts = {"pass": 0, "fail": 0, "skip": 0}
    for result in results:
        counts[result["status"].lower()] += 1
    summary = {"success": counts["fail"] == 0, **counts, "tests": results}
    if prefix is not None:
        summary["runPrefix"] = prefix + "/"
        summary["trackedCurrentRunPaths"] = tracked_paths
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
    return 1 if counts["fail"] else 0


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's ordinary message can echo arbitrary argument values.
        print_summary([{"name": "arguments", "status": "FAIL",
                        "reason": "Invalid arguments; use --help. No live calls were made."}])
        self.exit(2)


def argument_parser():
    parser = SafeArgumentParser(
        description="Opt-in live checks for one existing fns-skill-test-* vault. "
                    "Strongly recommend a token scoped only to that vault.",
        epilog="Provision secrets through a trusted environment, never CLI arguments. "
               "Required environment: FNS_BASE_URL, FNS_VAULT, FNS_AUTH_MODE=token, FNS_TOKEN. "
               "No scope override or vault/token provisioning is available. "
               "Use no concurrent writers. Notes use fns-live-<UUID>/ and remain after the run. "
               "Each CLI invocation and each complete paged scan has a 120-second bound.",
        allow_abbrev=False)
    parser.add_argument("--confirm-vault", required=True, metavar="EXACT_NAME",
                        help="Must exactly equal FNS_VAULT, whose name must start fns-skill-test-.")
    parser.add_argument("--allow-clear", action="store_true",
                        help="Authorize current-run single clearing and guarded full recycle clearing. "
                             "Full clearing is skipped unless the vault started empty; "
                             "the final scope scan is not atomic.")
    return parser


def checked_environment(arguments):
    # This is deliberately after argument parsing: --help does not read secrets.
    environment = os.environ.copy()
    vault = environment.get("FNS_VAULT", "")
    base = environment.get("FNS_BASE_URL", "")
    require(vault.startswith("fns-skill-test-") and len(vault) > len("fns-skill-test-"))
    require(arguments.confirm_vault == vault)
    require("\n" not in vault and "\r" not in vault)
    require(environment.get("FNS_AUTH_MODE") == "token")
    require(bool(environment.get("FNS_TOKEN")))
    parsed = urlsplit(base)
    require(parsed.scheme in ("http", "https") and bool(parsed.hostname))
    require(parsed.username is None and parsed.password is None)
    require(not parsed.query and not parsed.fragment)
    require(not any(character in base for character in ("\r", "\n", " ", "{", "}")))
    require(SCRIPT.is_file() and sys.version_info >= (3, 10) and os.name == "posix")
    # Check runtime imports only after scope/auth guards. --help and refused
    # scope never need dependencies and cannot start a client/network call.
    import requests
    import socks
    import urllib3
    version = tuple(int(part) for part in requests.__version__.split(".")[:3])
    urllib_version = tuple(int(part) for part in urllib3.__version__.split(".")[:3])
    require((2, 34, 2) <= version < (3, 0, 0))
    require((2, 8, 0) <= urllib_version < (3, 0, 0))
    require(callable(socks.socksocket))
    # Token-only calls need no login credentials or shell startup hooks.
    for key in ("FNS_USERNAME", "FNS_PASSWORD", "BASH_ENV", "ENV"):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class LiveChecks:
    READ_ONLY = frozenset(("doctor", "health", "version", "vaults", "list", "search"))
    NOTE_COMMANDS = frozenset(("read", "create", "upsert", "update", "append", "prepend",
                               "replace", "frontmatter", "rename", "delete", "restore"))

    def __init__(self, environment, allow_clear):
        self.environment = environment
        self.vault = environment["FNS_VAULT"]
        self.allow_clear = allow_clear
        self.prefix = "fns-live-" + uuid.uuid4().hex
        self.allocated = set()
        self.created = set()
        self.initial_paths = set()
        self.initially_empty = False
        self.full_clear_ready = False
        self.results = []
        # Literal %25 is part of the filename, not a path-normalization request.
        self.primary = self.allocate("Unicode 文/space + & %25 #.md")
        self.secondary = self.allocate("stdin.md")
        self.upsert_path = self.allocate("upsert.md")
        self.edit_path = self.allocate("frontmatter.md")
        self.primary_content = '# File source 文\nquoted "text" and \\slash\n\n'
        self.secondary_content = "# Stdin source 文\nfinal newlines stay\n\n"
        self.edit_body = "body-core\n"
        self.upsert_content = ""
        self.search_path = None
        self.search_content = None

    def allocate(self, label):
        path = self.prefix + "/" + label
        require(path not in self.allocated)
        require(all(part not in ("", ".", "..") for part in path.split("/")))
        self.allocated.add(path)
        return path

    def owned(self, path):
        require(path in self.allocated and path.startswith(self.prefix + "/"))
        require(path not in self.initial_paths)
        require(path in self.created)

    @staticmethod
    def option(arguments, name):
        require(arguments.count(name) == 1)
        position = arguments.index(name)
        require(position + 1 < len(arguments))
        return arguments[position + 1]

    def guard_call(self, command, arguments):
        require(self.environment.get("FNS_VAULT") == self.vault)
        require(self.environment.get("FNS_AUTH_MODE") == "token")
        require("--vault" not in arguments)
        if command in self.READ_ONLY:
            return
        if command == "recycle-clear" and "--all" in arguments:
            require(self.allow_clear and self.initially_empty and self.full_clear_ready)
            require(arguments == ("--all", "--confirm-vault", self.vault))
            self.full_clear_ready = False  # One authorization, one write; never a replay.
            return
        require(command in self.NOTE_COMMANDS or command == "recycle-clear")
        path = self.option(arguments, "--path")
        require(path in self.allocated and path.startswith(self.prefix + "/"))
        require(path not in self.initial_paths)
        if command in ("append", "prepend", "replace", "frontmatter", "delete", "restore", "recycle-clear"):
            self.owned(path)
        if command == "recycle-clear":
            require(self.allow_clear and arguments == ("--path", path, "--confirm"))
        if command == "rename":
            self.owned(self.option(arguments, "--old-path"))
            require(path not in self.created)

    @staticmethod
    def stop_child(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=3)

    def call(self, command, *arguments, stdin=None, exit_code=0, error_code=None,
             timeout=CALL_TIMEOUT):
        self.guard_call(command, arguments)
        require(timeout > 0)
        process = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), command, *arguments], env=self.environment,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            output, _ = process.communicate(
                input=None if stdin is None else stdin.encode("utf-8"), timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            self.stop_child(process)
            abort_test()
        require(process.returncode == exit_code)
        result = json.loads(output.decode("utf-8"))
        require(isinstance(result, dict))
        if exit_code == 0:
            require(set(result) == {"success", "code", "data"})
            require(result["success"] is True)
            require(type(result["code"]) is int and 1 <= result["code"] <= 6)
            return result["data"]
        require(set(result) == {"success", "error"} and result["success"] is False)
        error = result["error"]
        require(isinstance(error, dict))
        require(isinstance(error.get("type"), str) and bool(error["type"]))
        require(isinstance(error.get("message"), str))
        if error_code is not None:
            require(type(error.get("code")) is int and error["code"] == error_code)
        return None

    @staticmethod
    def page_data(data, page, size):
        require(isinstance(data, dict))
        rows, pager = data.get("list"), data.get("pager")
        require(isinstance(rows, list) and isinstance(pager, dict))
        require(type(pager.get("page")) is int and pager["page"] == page)
        require(type(pager.get("pageSize")) is int and pager["pageSize"] == size)
        require(type(pager.get("totalRows")) is int and pager["totalRows"] >= 0)
        total = pager["totalRows"]
        require(len(rows) == min(size, max(0, total - (page - 1) * size)))
        paths = []
        for row in rows:
            # Never strip, normalize, filter out, or silently ignore an entry.
            require(isinstance(row, dict) and isinstance(row.get("path"), str) and bool(row["path"]))
            paths.append(row["path"])
        return paths, total

    def scan_notes(self, recycle=False, keyword=None, size=100):
        deadline = time.monotonic() + SCAN_TIMEOUT
        paths = []
        total = None
        page = 1
        max_page = 1
        while page <= max_page:
            arguments = ["--search-mode", "path", "--sort-by", "path", "--sort-order", "asc",
                         "--page", str(page), "--page-size", str(size)]
            if recycle:
                arguments.append("--recycle")
            if keyword is not None:
                arguments.extend(("--keyword", keyword))
            data = self.call("list", *arguments, timeout=min(CALL_TIMEOUT, deadline - time.monotonic()))
            entries, page_total = self.page_data(data, page, size)
            if total is None:
                total = page_total
                max_page = max(1, (total + size - 1) // size)
                require(max_page <= MAX_SCAN_PAGES)
            require(page_total == total)
            paths.extend(entries)
            page += 1
        require(len(paths) == total and len(set(paths)) == total)
        return paths

    def note(self, path, recycle=False):
        arguments = ["--path", path]
        if recycle:
            arguments.append("--recycle")
        data = self.call("read", *arguments)
        require(isinstance(data, dict) and data.get("path") == path)
        require(isinstance(data.get("content"), str))
        return data

    def absent(self, path):
        require(path in self.allocated and path not in self.created and path not in self.initial_paths)
        self.call("read", "--path", path, exit_code=5, error_code=430)
        self.call("read", "--path", path, "--recycle", exit_code=5, error_code=430)

    def create_note(self, path, content):
        self.absent(path)
        self.call("create", "--path", path, "--content", content)
        self.created.add(path)
        require(self.note(path)["content"] == content)

    @staticmethod
    def frontmatter(content):
        lines = content.splitlines(keepends=True)
        require(bool(lines) and lines[0].rstrip("\r\n") == "---")
        end = next((index for index in range(1, len(lines))
                    if lines[index].rstrip("\r\n") == "---"), None)
        require(end is not None)
        return "".join(lines[1:end]), "".join(lines[end + 1:])

    def initial_snapshot(self):
        active = self.scan_notes()
        recycled = self.scan_notes(recycle=True)
        self.initial_paths = set(active) | set(recycled)
        require(not self.initial_paths.intersection(self.allocated))
        self.initially_empty = not active and not recycled

    def bare_percent_path_rejection(self):
        # Upstream 3.6.1 ValidatePath calls url.QueryUnescape: a bare percent
        # fails validation. The CLI must still transmit the original path.
        path = self.allocate("bare %.md")
        self.absent(path)
        created_before = set(self.created)
        self.call("create", "--path", path, "--content", "must not be written\n",
                  exit_code=5, error_code=444)
        self.absent(path)  # Both ordinary and recycle reads must still be 430.
        require(self.created == created_before and path not in self.created)

    def create_file(self):
        self.absent(self.primary)
        with tempfile.TemporaryDirectory(prefix="fns-live-content-") as directory:
            file = Path(directory) / "note.md"
            file.write_bytes(self.primary_content.encode("utf-8"))
            file.chmod(0o600)
            self.call("create", "--path", self.primary, "--content-file", str(file),
                      "--ctime", "1600000000123")
            self.created.add(self.primary)
        require(self.note(self.primary)["content"] == self.primary_content)

    def create_stdin(self):
        self.absent(self.secondary)
        self.call("create", "--path", self.secondary, "--stdin", stdin=self.secondary_content)
        self.created.add(self.secondary)
        require(self.note(self.secondary)["content"] == self.secondary_content)

    def list_paths(self):
        paths = self.scan_notes(keyword=self.prefix)
        require(set(paths) == {self.primary, self.secondary})

    def pagination(self):
        # Derive last page from the real pager.totalRows/pageSize, not data.total
        # or a guessed page count. Exactly two current-run notes exist here.
        paths = self.scan_notes(keyword=self.prefix, size=1)
        require(len(paths) == 2 and set(paths) == {self.primary, self.secondary})

    def duplicate_create(self):
        before = self.note(self.primary)
        self.call("create", "--path", self.primary, "--content", "must not overwrite",
                  exit_code=5, error_code=431)
        after = self.note(self.primary)
        require(after["content"] == before["content"] and after.get("ctime") == before.get("ctime"))

    def update_creation_time(self):
        before = self.note(self.primary)
        require(type(before.get("ctime")) is int and before["ctime"] >= 0)
        replacement = "# Updated full content 文\n\n"
        self.call("update", "--path", self.primary, "--content", replacement)
        after = self.note(self.primary)
        require(after["content"] == replacement and after.get("ctime") == before["ctime"])
        self.primary_content = replacement

    def expected_hash_guard(self):
        before = self.note(self.primary)
        require(isinstance(before.get("contentHash"), str) and bool(before["contentHash"]))
        mismatch = "mismatch-" + uuid.uuid4().hex
        require(mismatch != before["contentHash"])
        self.call("update", "--path", self.primary, "--content", "must not be written",
                  "--expect-hash", mismatch, exit_code=6)
        after = self.note(self.primary)
        require(after["content"] == before["content"] and after.get("contentHash") == before["contentHash"])
        require(after.get("ctime") == before.get("ctime"))

    def missing_update(self):
        path = self.allocate("missing-update.md")
        self.absent(path)
        self.call("update", "--path", path, "--content", "must not create",
                  exit_code=5, error_code=430)
        self.call("read", "--path", path, exit_code=5, error_code=430)

    def upsert(self):
        self.absent(self.upsert_path)
        self.call("upsert", "--path", self.upsert_path, "--content", "upsert creates\n")
        self.created.add(self.upsert_path)
        require(self.note(self.upsert_path)["content"] == "upsert creates\n")
        self.upsert_content = "needle needle\nitem12 item34\n"
        self.call("upsert", "--path", self.upsert_path, "--content", self.upsert_content)
        require(self.note(self.upsert_path)["content"] == self.upsert_content)

    def append_prepend(self):
        initial = "---\ntitle: live-title\ndraft: true\n---\n" + self.edit_body
        self.create_note(self.edit_path, initial)
        tail = "appended-tail\n"
        self.call("append", "--path", self.edit_path, "--content", tail)
        _, body = self.frontmatter(self.note(self.edit_path)["content"])
        require(body == self.edit_body + tail)
        head = "prepended-head\n"
        self.call("prepend", "--path", self.edit_path, "--content", head)
        header, body = self.frontmatter(self.note(self.edit_path)["content"])
        require(re.search(r"(?m)^title: (?:live-title|\"live-title\"|'live-title')[ \t]*$", header) is not None)
        require(body == head + self.edit_body + tail)
        self.edit_body = body

    def frontmatter_types(self):
        updates = {"title": "live-title-updated", "enabled": True, "count": 7,
                   "draft": "remove wins", "removeMe": "discard"}
        self.call("frontmatter", "--path", self.edit_path,
                  "--updates", json.dumps(updates), "--remove", '["draft","removeMe"]')
        header, body = self.frontmatter(self.note(self.edit_path)["content"])
        # This fixture uses only flat YAML scalars. Unquoted true/7 distinguish
        # a boolean/integer from the strings "true"/"7" without a YAML package.
        require(re.search(r"(?m)^enabled: true[ \t]*$", header) is not None)
        require(re.search(r"(?m)^count: 7[ \t]*$", header) is not None)
        require(re.search(r"(?m)^title: (?:live-title-updated|\"live-title-updated\"|'live-title-updated')[ \t]*$", header) is not None)
        require(re.search(r"(?m)^(?:draft|removeMe):", header) is None)
        require(body == self.edit_body)

    def literal_replace(self):
        self.call("replace", "--path", self.upsert_path, "--find", "needle",
                  "--replace", "--replacement", "--all")
        self.upsert_content = "--replacement --replacement\nitem12 item34\n"
        require(self.note(self.upsert_path)["content"] == self.upsert_content)

    def regex_replace(self):
        self.call("replace", "--path", self.upsert_path, "--find", r"item([0-9]+)",
                  "--replace", "value-${1}", "--regex", "--all")
        self.upsert_content = "--replacement --replacement\nvalue-12 value-34\n"
        require(self.note(self.upsert_path)["content"] == self.upsert_content)

    def replacement_errors(self):
        self.call("replace", "--path", self.upsert_path, "--find", "missing-" + uuid.uuid4().hex,
                  "--replace", "unused", "--fail-if-no-match", exit_code=5, error_code=442)
        require(self.note(self.upsert_path)["content"] == self.upsert_content)
        self.call("replace", "--path", self.upsert_path, "--find", "[", "--replace", "unused",
                  "--regex", exit_code=5, error_code=443)
        require(self.note(self.upsert_path)["content"] == self.upsert_content)

    def rename(self):
        old = self.primary
        new = self.allocate("renamed 文 + & %25.md")
        self.absent(new)
        before = self.note(old)["content"]
        self.call("rename", "--old-path", old, "--path", new)
        self.created.add(new)
        self.primary = new
        self.call("read", "--path", old, exit_code=5, error_code=430)
        require(self.note(new)["content"] == before)

    def soft_delete_restore(self):
        before = self.note(self.secondary)["content"]
        self.call("delete", "--path", self.secondary)
        self.call("read", "--path", self.secondary, exit_code=5, error_code=430)
        require(self.note(self.secondary, recycle=True)["content"] == before)
        self.call("restore", "--path", self.secondary)
        require(self.note(self.secondary)["content"] == before)

    def content_search(self):
        self.search_path = self.allocate("content-search.md")
        term = "fnsbody" + uuid.uuid4().hex
        require(term not in self.search_path)
        self.search_content = "# Body-only search fixture\n" + term + "\n"
        self.create_note(self.search_path, self.search_content)
        require(self.scan_notes(keyword=term) == [])
        for attempt in range(SEARCH_ATTEMPTS):
            data = self.call("search", "--keyword", term, "--search-mode", "content",
                             "--page", "1", "--page-size", "100")
            paths, _ = self.page_data(data, 1, 100)
            if self.search_path in paths:
                require(term in self.note(self.search_path)["content"] and term not in self.search_path)
                return
            if attempt + 1 < SEARCH_ATTEMPTS:
                time.sleep(1)
        raise SkippedCheck("search_not_ready")

    def single_recycle_clear(self):
        if not self.allow_clear:
            raise SkippedCheck("not_authorized")
        path = self.allocate("clear-single.md")
        self.create_note(path, "single clear fixture\n")
        self.call("delete", "--path", path)
        require(self.note(path, recycle=True)["content"] == "single clear fixture\n")
        self.call("recycle-clear", "--path", path, "--confirm")
        require(path not in self.scan_notes(recycle=True, keyword=self.prefix))

    def full_recycle_clear(self):
        if not self.allow_clear:
            raise SkippedCheck("not_authorized")
        if not self.initially_empty:
            raise SkippedCheck("not_initially_empty")
        paths = [self.allocate("clear-full-1.md"), self.allocate("clear-full-2.md")]
        for path in paths:
            self.create_note(path, "full clear fixture\n")
            self.call("delete", "--path", path)
        # No keyword/prefix filter and no dropped entries: check every page of
        # the entire recycle bin. The next network operation is the one DELETE.
        recycled = self.scan_notes(recycle=True)
        require(set(paths).issubset(recycled))
        for path in recycled:
            self.owned(path)
        self.full_clear_ready = True
        self.call("recycle-clear", "--all", "--confirm-vault", self.vault)
        require(self.scan_notes(recycle=True) == [])

    def retained_notes(self):
        expected = {self.primary: self.primary_content, self.secondary: self.secondary_content,
                    self.upsert_path: self.upsert_content}
        if self.search_path is not None:
            expected[self.search_path] = self.search_content
        for path, content in expected.items():
            require(self.note(path)["content"] == content)
        _, body = self.frontmatter(self.note(self.edit_path)["content"])
        require(body == self.edit_body)

    def run(self):
        cases = [
            ("doctor", lambda: self.call("doctor")),
            ("health", lambda: self.call("health")),
            ("version", lambda: self.call("version")),
            ("vaults", lambda: self.call("vaults")),
            ("initial_unfiltered_scope_snapshot", self.initial_snapshot),
            ("known_bare_percent_path_rejection_444_no_creation", self.bare_percent_path_rejection),
            ("unicode_special_literal_percent25_path_file_trailing_newlines", self.create_file),
            ("stdin_trailing_newlines", self.create_stdin),
            ("list_path_search", self.list_paths),
            ("server_pager_size_one_two_notes", self.pagination),
            ("duplicate_create_431_no_overwrite", self.duplicate_create),
            ("update_preserves_ctime", self.update_creation_time),
            ("expect_hash_exit_6_no_change", self.expected_hash_guard),
            ("missing_update_430_no_creation", self.missing_update),
            ("upsert_create_and_overwrite", self.upsert),
            ("append_prepend_after_frontmatter", self.append_prepend),
            ("frontmatter_types_remove_wins", self.frontmatter_types),
            ("literal_replace_all", self.literal_replace),
            ("go_regex_replace_all", self.regex_replace),
            ("replace_no_match_442_invalid_regex_443", self.replacement_errors),
            ("rename_literal_percent25_old_430_new_content_equal", self.rename),
            ("soft_delete_430_recycle_read_restore", self.soft_delete_restore),
            ("body_only_content_search", self.content_search),
            ("single_recycle_clear_current_run_only", self.single_recycle_clear),
            ("full_recycle_clear_empty_vault_complete_scope_check", self.full_recycle_clear),
            ("final_notes_retained", self.retained_notes),
        ]
        self.results.append({"name": "scope_guard", "status": "PASS"})
        aborted = False
        for name, check in cases:
            if aborted:
                self.results.append({"name": name, "status": "SKIP", "reason": SKIP_REASONS["aborted"]})
                continue
            try:
                check()
            except SkippedCheck as skipped:
                self.results.append({"name": name, "status": "SKIP",
                                     "reason": SKIP_REASONS.get(skipped.reason, SKIP_REASONS["aborted"])})
            except (Exception, KeyboardInterrupt):
                # Never stringify exceptions, subprocess commands, stderr, or
                # returned JSON: even a hostile server cannot leak into reports.
                self.results.append({"name": name, "status": "FAIL", "reason": FAIL_REASON})
                aborted = True
            else:
                self.results.append({"name": name, "status": "PASS"})
        return print_summary(self.results, self.prefix, len(self.created))


def main(argv=None):
    try:
        arguments = argument_parser().parse_args(argv)
    except (Exception, KeyboardInterrupt):
        print_summary([{"name": "arguments", "status": "FAIL",
                        "reason": "Argument parsing failed or was interrupted. No live calls were made."}])
        return 2
    try:
        checks = LiveChecks(checked_environment(arguments), arguments.allow_clear)
    except (Exception, KeyboardInterrupt):
        print_summary([{"name": "scope_guard", "status": "FAIL", "reason": GUARD_REASON}])
        return 2
    try:
        return checks.run()
    except (Exception, KeyboardInterrupt):
        checks.results.append({"name": "runner", "status": "FAIL", "reason": FAIL_REASON})
        return print_summary(checks.results, checks.prefix, len(checks.created))


if __name__ == "__main__":
    sys.exit(main())
