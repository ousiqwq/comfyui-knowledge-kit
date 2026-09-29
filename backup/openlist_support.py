#!/usr/bin/env python3
"""Hermes -> OpenList, Python 3.10+, standard library only. See backup-config.json."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from zoneinfo import ZoneInfo


NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
FILE_RE = re.compile(r"hermes-(regular|checkpoint)-\d{8}T\d{6}Z-[a-f0-9]{32}\.zip\Z")
HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    pass


def log(message):
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest_file(path):
    h = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as src:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as dst:
            dst.write(json_bytes(value))
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            folder_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(folder_fd)
            finally:
                os.close(folder_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owned_local_file(path, directory):
    path, directory = Path(path), Path(directory).resolve()
    if path.is_symlink() or path.resolve().parent != directory:
        raise BackupError(f"Refusing local path outside the working directory: {path}")
    if path.exists() and not path.is_file():
        raise BackupError(f"Not a regular local file: {path}")
    return path


@contextlib.contextmanager
def process_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+b") as lock:
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BackupError("Another backup process is already running; nothing changed.") from exc
        yield


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackupError("API redirected. Set openlist.url to the final server URL.")


class OpenList:
    def __init__(self, config):
        self.config = config
        self.base = config["url"].rstrip("/")
        parsed = urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query:
            raise BackupError("Invalid openlist.url")
        self.token = None
        self.opener = build_opener(NoRedirect())
        self.timeout = int(config.get("transfer_timeout_seconds", 21600))
        self.attempts = int(config.get("verify_attempts", 12))
        self.pause = float(config.get("retry_delay_seconds", 5))
        self.directory_attempts = int(config.get("directory_ready_attempts", 24))
        self.directory_pause = float(config.get("directory_ready_delay_seconds", 5))
        if self.timeout < 60:
            raise BackupError("openlist.transfer_timeout_seconds must be at least 60.")
        if not 1 <= self.attempts <= 120 or not 0 <= self.pause <= 300:
            raise BackupError("OpenList verification retry settings are outside safe bounds.")
        if not 1 <= self.directory_attempts <= 120 or not 0 <= self.directory_pause <= 300:
            raise BackupError("OpenList directory retry settings are outside safe bounds.")

    def api(self, endpoint, payload=None, method="POST", stream=None, headers=None):
        request_headers = {"Accept": "application/json"}
        if self.token:
            request_headers["Authorization"] = self.token
        if stream is None and payload is not None:
            stream = json_bytes(payload)
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(self.base + "/api/" + endpoint, data=stream,
                          headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout if endpoint == "fs/put" else 90) as response:
                result = json.loads(response.read(4 * CHUNK))
        except HTTPError as exc:
            raise BackupError(f"OpenList {endpoint}: HTTP {exc.code}") from exc
        except (OSError, URLError, ValueError) as exc:
            raise BackupError(f"OpenList {endpoint}: network/JSON error ({type(exc).__name__})") from exc
        if result.get("code") != 200:
            message = str(result.get("message", "unknown error"))[:300]
            for secret in (self.token, self.config.get("password")):
                if secret:
                    message = message.replace(secret, "[hidden]")
            raise BackupError(f"OpenList {endpoint}: code {result.get('code')}, {message}")
        return result.get("data")

    def login(self):
        result = self.api("auth/login", {"username": self.config["username"],
                                        "password": self.config["password"], "otp_code": ""})
        self.token = result["token"]
        me = self.api("me", method="GET")
        expected = self.config["expected_base_path"].rstrip("/") or "/"
        if (me.get("base_path", "").rstrip("/") or "/") != expected:
            raise BackupError("Account base_path does not match expected_base_path; refusing access.")
        permissions = int(me.get("permission", 0))
        if me.get("role") != 2 and (permissions & (1 << 3) == 0 or permissions & (1 << 7) == 0):
            raise BackupError("Account requires upload/create-directory and delete permissions.")
        log(f"OpenList login OK: {me['username']}; base_path={expected}")

    def listing(self, directory):
        rows = []
        page = 1
        while True:
            result = self.api("fs/list", {"path": directory, "password": "", "page": page,
                                          "per_page": 200, "refresh": page == 1})
            batch = result.get("content") or []
            rows.extend(batch)
            if len(rows) >= int(result.get("total", 0)):
                break
            if not batch:
                raise BackupError("Directory pagination incomplete; refusing cleanup.")
            page += 1
            if page > 10000:
                raise BackupError("Unexpected directory size.")
        names = [row["name"] for row in rows]
        if len(names) != len(set(names)):
            raise BackupError("Duplicate remote names; refusing ambiguous operations.")
        return rows

    def entry(self, path):
        parent, name = posixpath.split(path)
        return next((row for row in self.listing(parent or "/") if row["name"] == name), None)

    def wait_directory_ready(self, directory):
        """Wait for cloud-backed OpenList storage to expose a directory just created via fs/mkdir."""
        attempts = self.directory_attempts
        delay = self.directory_pause
        last_error = None
        for attempt in range(attempts):
            try:
                self.listing(directory)
                return
            except BackupError as exc:
                message = str(exc).lower()
                if "object not found" not in message and "failed get dir" not in message:
                    raise
                last_error = exc
                if attempt + 1 < attempts:
                    if attempt == 0:
                        log(f"Waiting for newly-created remote directory to become readable: {directory}")
                    time.sleep(delay)
        raise BackupError(
            f"Remote directory was created but did not become readable within {attempts * delay:g}s: "
            f"{directory}; retry is safe ({last_error})"
        )

    def mkdir(self, directory):
        current = ""
        for name in PurePosixPath(directory).parts[1:]:
            current += "/" + name
            found = self.entry(current)
            if found is None:
                self.api("fs/mkdir", {"path": current})
                self.wait_directory_ready(current)
            elif not found["is_dir"]:
                raise BackupError(f"Remote path is a file: {current}")

    def download(self, path, capture=False):
        info = self.api("fs/get", {"path": path, "password": ""})
        if info.get("is_dir") or not info.get("raw_url"):
            raise BackupError(f"Remote file cannot be downloaded: {path}")
        target = info["raw_url"]
        parsed = urlsplit(target)
        if parsed.scheme not in ("http", "https") or parsed.username:
            raise BackupError("Unexpected download URL scheme.")
        # No OpenList Authorization header is sent to raw_url or redirect destinations.
        request = Request(target, headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"})
        sha = hashlib.sha256()
        size = 0
        chunks = []
        try:
            with urlopen(request, timeout=self.timeout) as response:
                for block in iter(lambda: response.read(CHUNK), b""):
                    sha.update(block)
                    size += len(block)
                    if capture:
                        if size > 4 * CHUNK:
                            raise BackupError("Remote metadata unexpectedly large.")
                        chunks.append(block)
        except (OSError, URLError) as exc:
            raise BackupError(f"Download failed: {path} ({type(exc).__name__})") from exc
        if size != int(info["size"]):
            raise BackupError(f"Incomplete download: {path}")
        return size, sha.hexdigest(), b"".join(chunks) if capture else None

    def verify(self, path, size, sha):
        last_error = None
        for attempt in range(self.attempts):
            try:
                found = self.entry(path)
                if found is None or found["is_dir"] or int(found["size"]) != size:
                    raise BackupError(f"Remote size/type mismatch: {path}")
                actual_size, actual_sha, _ = self.download(path)
                if (actual_size, actual_sha) != (size, sha):
                    raise BackupError(f"SHA-256 mismatch: {path}; old backups will be retained.")
                return
            except BackupError as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    time.sleep(self.pause)
        raise last_error

    def upload(self, path, source):
        if isinstance(source, bytes):
            size, sha = len(source), hashlib.sha256(source).hexdigest()
        else:
            size, sha = digest_file(source)
        existing = self.entry(path)
        if existing is None:
            log(f"Uploading {path} ({size:,} bytes)")
            with contextlib.ExitStack() as stack:
                body = io.BytesIO(source) if isinstance(source, bytes) else stack.enter_context(Path(source).open("rb"))
                try:
                    result = self.api("fs/put", method="PUT", stream=body, headers={
                        "File-Path": quote(path, safe="/"), "Content-Length": str(size),
                        "Content-Type": "application/octet-stream", "As-Task": "false",
                        "Overwrite": "false", "X-File-Sha256": sha,
                    })
                    if result and result.get("task"):
                        raise BackupError("Unexpected asynchronous upload; not treating task submission as success.")
                except BackupError as exc:
                    # Ambiguous timeout may have occurred after upload; read back exact bytes.
                    log(f"Upload response uncertain; checking remote copy: {exc}")
        log(f"Downloading for full SHA-256 verification: {path}")
        self.verify(path, size, sha)
        return size, sha

    def remove_file(self, path, size, sha):
        existing = self.entry(path)
        if existing is None:
            return
        # A file that was replaced by something else (including a directory) is never deleted.
        self.verify(path, size, sha)
        directory, name = posixpath.split(path)
        self.api("fs/remove", {"dir": directory or "/", "names": [name]})
        for attempt in range(self.attempts):
            if self.entry(path) is None:
                log(f"Deleted verified old file: {path}")
                return
            if attempt + 1 < self.attempts:
                time.sleep(self.pause)
        raise BackupError(f"Delete not confirmed: {path}")
