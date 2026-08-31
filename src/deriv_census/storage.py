"""Append-only, crash-safe capture of everything the census observes.

Format is newline-delimited JSON, partitioned by UTC date and stream. JSONL is
chosen over Parquet for the write path deliberately: a fourteen-day unattended
run will be interrupted -- by a laptop lid, a power cut, an OS update -- and a
partially written JSONL file loses at most its final line, whereas a partially
written columnar file can lose the whole partition.

Analysis reads JSONL and can export Parquet afterwards, which gives the
columnar format where it helps (repeated analysis) and avoids it where it hurts
(the unattended writer).

Raw payloads are preserved alongside parsed fields. If a field name on the live
API differs from what this code expects, the run is still salvageable from the
raw record instead of being fourteen days wasted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

PROPOSALS = "proposals"
TICKS = "ticks"
EVENTS = "events"
STREAMS = (PROPOSALS, TICKS, EVENTS)


def utc_date(ts_ms: int | None = None) -> str:
    ts = (ts_ms / 1000.0) if ts_ms is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class WriterStats:
    written: dict[str, int]

    def total(self) -> int:
        return sum(self.written.values())


class CensusStore:
    """One writer per process. Not safe for concurrent processes on one root."""

    def __init__(self, root: str | Path, flush_interval_s: float = 5.0) -> None:
        self.root = Path(root)
        self._flush_interval = flush_interval_s
        self._handles: dict[tuple[str, str], Any] = {}
        self._last_flush = time.monotonic()
        self.stats = WriterStats(written={s: 0 for s in STREAMS})
        for stream in STREAMS:
            (self.root / stream).mkdir(parents=True, exist_ok=True)

    def _handle(self, stream: str, date: str):
        key = (stream, date)
        handle = self._handles.get(key)
        if handle is None:
            path = self.root / stream / f"{date}.jsonl"
            handle = path.open("a", encoding="utf-8")
            self._handles[key] = handle
        return handle

    def write(self, stream: str, record: dict[str, Any]) -> None:
        if stream not in STREAMS:
            raise ValueError(f"unknown stream {stream!r}")
        record.setdefault("ts_ms", int(time.time() * 1000))
        handle = self._handle(stream, utc_date(record["ts_ms"]))
        handle.write(json.dumps(record, separators=(",", ":"),
                                default=str) + "\n")
        self.stats.written[stream] += 1
        if time.monotonic() - self._last_flush >= self._flush_interval:
            self.flush()

    def event(self, kind: str, **fields: Any) -> None:
        """Record an operational event. These make a run auditable after the
        fact: how long it actually ran, how many reconnects, what was skipped
        and why. A census whose coverage cannot be reconstructed is not
        evidence."""
        self.write(EVENTS, {"kind": kind, **fields})

    def flush(self) -> None:
        """Flush and fsync every open handle.

        fsync matters: without it a power loss can leave the file with
        buffered data lost even though the process wrote it.
        """
        for handle in self._handles.values():
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except (OSError, ValueError) as exc:
                log.warning("flush failed: %s", exc)
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self.flush()
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()

    def __enter__(self) -> "CensusStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_stream(root: str | Path, stream: str,
                strict: bool = False) -> Iterator[dict[str, Any]]:
    """Yield every record in a stream, oldest partition first.

    A truncated final line -- the expected outcome of an interrupted run -- is
    skipped with a warning rather than aborting the read, unless ``strict``.
    """
    directory = Path(root) / stream
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    if strict:
                        raise
                    log.warning("skipping malformed line %s:%d", path, lineno)


def export_parquet(root: str | Path, stream: str,
                   destination: str | Path) -> int:
    """Materialise a stream as Parquet for repeated analysis. Returns row count."""
    import pandas as pd

    rows = list(read_stream(root, stream))
    if not rows:
        return 0
    frame = pd.DataFrame(rows)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(destination, index=False)
    return len(frame)
