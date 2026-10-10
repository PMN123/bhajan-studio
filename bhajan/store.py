"""Crash- and thread-safe cache files.

Every cache file is written to a unique temporary file in the same folder and then renamed into
place, so a reader never sees half a file and a killed process never leaves a truncated one behind.
Readers treat an unreadable file as a cache miss.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Callable

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock(key: str) -> threading.Lock:
    """One lock per key (e.g. a file path), shared by every thread in this process."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def write_with(path: str, write: Callable[[str], None]) -> None:
    """Call write(tmp_path) to produce the file, then atomically move it to `path`."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{stem}.", suffix=f".part{ext}")
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_bytes(path: str, data: bytes) -> None:
    def _w(tmp: str) -> None:
        with open(tmp, "wb") as f:
            f.write(data)
    write_with(path, _w)


def write_json(path: str, obj: Any) -> None:
    write_bytes(path, json.dumps(obj).encode())


def read_json(path: str) -> Any | None:
    """The parsed file, or None if it is missing or damaged (a damaged file is removed)."""
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
