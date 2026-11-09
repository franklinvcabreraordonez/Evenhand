"""An append-only record of what the proxy did, one JSON object per line.

It is also the proxy's journal: when the proxy starts it reads this file to find receipts that were queued
but never recorded, and calls that were in flight when it last stopped."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List


class AuditLog:
    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._fh = open(path, "ab")

    def event(self, kind: str, **fields: Any) -> None:
        record: Dict[str, Any] = {"ts": round(self._clock(), 3), "event": kind}
        record.update(fields)
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    @staticmethod
    def read(path: str) -> List[Dict[str, Any]]:
        """Every readable record, oldest first. A damaged line is skipped, not fatal."""
        records: List[Dict[str, Any]] = []
        try:
            with open(path, "rb") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except FileNotFoundError:
            pass
        return records