"""A small durable log for the witness: its signing key and everything it has recorded.

One folder holds two files:
  witness.key   the witness's private signing key, 32 random bytes as hex. Keep it secret and back it up.
  log.jsonl     one JSON record per line. Each record is written and flushed to disk before the witness replies.

Run one witness per folder. The log can be rebuilt into a witness only if every receipt in it still verifies.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import List, Optional, Tuple, Union

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import Signer


class StoreError(Exception):
    """The folder cannot be used, or a write to it failed."""


class LogCorrupt(StoreError):
    """The saved log does not verify, so the witness refuses to start from it."""


class Store:
    def __init__(self, directory: Union[str, Path]):
        self.dir = Path(directory)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(f"cannot use the data folder {self.dir}: {exc}")
        self.key_path = self.dir / "witness.key"
        self.log_path = self.dir / "log.jsonl"
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0

    # ----- the witness's key -----
    def signer(self) -> Signer:
        """Load the witness key, or create it the first time. An existing key is never overwritten."""
        if self.key_path.exists():
            try:
                seed = bytes.fromhex(self.key_path.read_text(encoding="ascii").strip())
                if len(seed) != 32:
                    raise ValueError("not 32 bytes")
                return Signer(Ed25519PrivateKey.from_private_bytes(seed))
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                raise StoreError(f"{self.key_path} is not a valid witness key ({exc}). It was left untouched.")
        seed = os.urandom(32)
        try:
            fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(seed.hex() + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            raise StoreError(f"cannot save the witness key to {self.key_path}: {exc}")
        return Signer(Ed25519PrivateKey.from_private_bytes(seed))

    # ----- the log -----
    def open_log(self) -> List[Tuple[int, dict]]:
        """Read the saved records, repair a last line that was cut off mid-write, and open the log for appending.

        Returns (line number, record) pairs in order. A bad line anywhere but the end raises LogCorrupt."""
        data = self.log_path.read_bytes() if self.log_path.exists() else b""
        records: List[Tuple[int, dict]] = []
        offset, lineno, keep, needs_newline = 0, 0, len(data), False
        while offset < len(data):
            nl = data.find(b"\n", offset)
            end = len(data) if nl == -1 else nl
            lineno += 1
            try:
                rec = json.loads(data[offset:end].decode("utf-8"))
                if not isinstance(rec, dict):
                    raise ValueError("not a JSON object")
            except (ValueError, UnicodeDecodeError) as exc:
                if nl == -1:  # the last line, cut off while it was being written: it was never acknowledged
                    keep = offset
                    break
                raise LogCorrupt(f"line {lineno} of {self.log_path} is not a valid record ({exc})")
            records.append((lineno, rec))
            if nl == -1:
                needs_newline = True
            offset = len(data) if nl == -1 else nl + 1
        try:
            if keep != len(data):
                with open(self.log_path, "r+b") as f:
                    f.truncate(keep)
            self._fh = open(self.log_path, "ab")
            if needs_newline:
                self._fh.write(b"\n")
                self._fh.flush()
                os.fsync(self._fh.fileno())
            self._size = os.fstat(self._fh.fileno()).st_size
        except OSError as exc:
            raise StoreError(f"cannot open the log {self.log_path} for writing: {exc}")
        return records

    def append(self, record: dict) -> None:
        """Write one record and force it to disk. Raises StoreError, having written nothing, if that fails."""
        if self._fh is None:
            raise StoreError("the log is not open")
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        with self._lock:
            try:
                if os.fstat(self._fh.fileno()).st_size != self._size:
                    raise StoreError("the log was changed by something else while the witness was running; "
                                     "run one witness per data folder")
                self._fh.write(data)
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._size += len(data)
            except OSError as exc:
                try:
                    self._fh.truncate(self._size)
                except (OSError, ValueError):
                    pass
                raise StoreError(f"could not save to the log: {exc}")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None