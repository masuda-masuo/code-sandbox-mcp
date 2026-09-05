"""Bounded process-local snapshots of sanitized foreground exec output."""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .output_control import build_output_envelope, count_lines, sanitize_output


@dataclass(frozen=True)
class _Entry:
    container_id: str
    text: str
    created: float
    size: int


class OutputStore:
    """Expire after one hour; evict oldest entries before exceeding limits."""

    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024,
                 max_entry_bytes: int = 8 * 1024 * 1024,
                 max_entries: int = 128, ttl: float = 3600) -> None:
        if min(max_bytes, max_entry_bytes, max_entries, ttl) <= 0:
            raise ValueError("output store limits must be positive")
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.max_entries = max_entries
        self.ttl = ttl
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if now - entry.created < self.ttl:
                break
            self._entries.pop(key)
            self._bytes -= entry.size

    def put(self, container_id: str, text: str) -> str | None:
        """Return an opaque ID, or None when the full snapshot is too large."""
        clean = sanitize_output(text)
        size = len(clean.encode("utf-8"))
        if size > min(self.max_bytes, self.max_entry_bytes):
            return None
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            while self._entries and (
                self._bytes + size > self.max_bytes
                or len(self._entries) >= self.max_entries
            ):
                _, old = self._entries.popitem(last=False)
                self._bytes -= old.size
            key = secrets.token_hex(16)
            self._entries[key] = _Entry(container_id, clean, now, size)
            self._bytes += size
            return key

    def read(self, container_id: str, output_id: str, *, offset: int = 0,
             limit: int = 100, tail_lines: int | None = None) -> dict:
        """Read without Docker or command execution; never silently lose an ID."""
        if offset < 0 or not 1 <= limit <= 1000:
            return {"status": "error", "error": "offset >= 0 and 1 <= limit <= 1000 required"}
        if tail_lines is not None and (offset != 0 or not 1 <= tail_lines <= 1000):
            return {"status": "error", "error": "tail_lines requires offset=0 and 1..1000"}
        with self._lock:
            self._expire(time.monotonic())
            entry = self._entries.get(output_id)
            if entry is None or entry.container_id != container_id:
                return {"status": "error", "error": (
                    "Output ID unknown, expired, evicted, or belongs to another container; "
                    "snapshots do not survive server restart."
                )}
        total = count_lines(entry.text)
        if tail_lines is not None:
            # Match the foreground stream's line-count convention.
            offset = max(0, total - tail_lines)
            limit = tail_lines
        page = build_output_envelope(entry.text, total_lines=total,
                                     offset=offset, limit=limit)
        return {"status": "ok", "output_id": output_id, "offset": offset,
                "output": page.output, "shown": page.shown,
                "total_lines": page.total_lines, "truncated": page.truncated,
                "next_offset": page.next_offset, "has_more": page.has_more}


OUTPUT_STORE = OutputStore()
