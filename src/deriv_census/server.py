"""A local dashboard for watching a capture fill up.

Fourteen days is a long time to stare at a log file. This serves the same
report the CLI prints, as a page that refreshes itself, plus a live header
showing how much has been captured and whether the run is still healthy.

Two deliberate constraints:

* **Standard library only.** No web framework, no build step, no extra
  dependency to install on the machine that has to stay up for a fortnight.
* **Bound to localhost by default.** The page exposes market data and a
  verdict, not credentials -- there are none -- but a capture box should not
  be listening on a public interface without the operator choosing that.

The report is cached with a short TTL because building it reads the whole
capture, which grows to gigabytes. A dashboard that re-reads several GB on
every browser refresh would compete with the capture it is meant to observe.
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .analysis import INSUFFICIENT, CensusReport, build_report
from .config import CensusConfig
from .report import VERDICT_COLOUR, render_html
from .storage import EVENTS, PROPOSALS, TICKS

log = logging.getLogger(__name__)

DEFAULT_TTL_S = 60.0


def tail_jsonl(path: Path, limit: int = 40, block: int = 65536) -> list[dict]:
    """Read the last few records of a JSONL file without loading it all.

    The events file grows all run; seeking from the end keeps the dashboard's
    cost independent of how long the capture has been going.
    """
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            read = min(size, block)
            handle.seek(size - read)
            chunk = handle.read(read).decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in chunk.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a partial first or last line; expected
    return out


def latest_events(root: Path, limit: int = 40) -> list[dict]:
    directory = root / EVENTS
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.jsonl"))
    return tail_jsonl(files[-1], limit) if files else []


def capture_health(root: Path) -> dict[str, Any]:
    """Cheap liveness summary, computed without reading the whole capture."""
    events = latest_events(root)
    heartbeats = [e for e in events if e.get("kind") == "heartbeat"]
    last = heartbeats[-1] if heartbeats else None
    finished = any(e.get("kind") == "run_finished" for e in events)

    age = None
    if last and last.get("ts_ms"):
        age = max(0.0, time.time() - last["ts_ms"] / 1000.0)

    def dir_bytes(stream: str) -> int:
        directory = root / stream
        if not directory.exists():
            return 0
        return sum(p.stat().st_size for p in directory.glob("*.jsonl"))

    if finished:
        state = "finished"
    elif age is None:
        state = "starting"
    elif age < 300:
        state = "running"
    else:
        state = "stalled"

    return {
        "state": state,
        "seconds_since_heartbeat": round(age, 1) if age is not None else None,
        "proposals_recorded": (last or {}).get("proposals_recorded"),
        "ticks_recorded": (last or {}).get("ticks_recorded"),
        "rotations": (last or {}).get("rotations"),
        "elapsed_hours": (last or {}).get("elapsed_hours"),
        "reconnects": ((last or {}).get("client") or {}).get("reconnects"),
        "bytes_on_disk": dir_bytes(PROPOSALS) + dir_bytes(TICKS) + dir_bytes(EVENTS),
        "recent_events": [e.get("kind") for e in events[-8:]],
    }


@dataclass
class _Cached:
    report: CensusReport | None = None
    built_at: float = 0.0
    build_seconds: float = 0.0
    error: str | None = None


class ReportCache:
    """Builds the report at most once per TTL, shared across requests."""

    def __init__(self, config: CensusConfig, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._config = config
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._state = _Cached()

    def get(self) -> _Cached:
        with self._lock:
            fresh = (self._state.report is not None
                     and time.time() - self._state.built_at < self._ttl)
            if fresh:
                return self._state
            started = time.time()
            try:
                report = build_report(self._config.storage.root,
                                      self._config.decision)
                self._state = _Cached(report, time.time(),
                                      time.time() - started, None)
            except Exception as exc:  # noqa: BLE001 - never take the page down
                log.warning("report build failed: %s", exc)
                self._state = _Cached(self._state.report, time.time(),
                                      time.time() - started, str(exc))
            return self._state


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


STATE_COLOUR = {"running": "#1a7f37", "starting": "#9a6700",
                "stalled": "#cf222e", "finished": "#0969da"}


def render_page(cached: _Cached, health: dict[str, Any],
                refresh_s: int) -> str:
    report = cached.report
    state = health["state"]
    colour = STATE_COLOUR.get(state, "#57606a")

    if report is None:
        body = ("<p class='muted'>No capture data yet. The page refreshes "
                "itself; leave it open.</p>")
        verdict, vcolour = "WAITING", "#57606a"
    else:
        # Reuse the CLI's own renderer so the dashboard and the terminal can
        # never disagree about what the numbers are.
        full = render_html(report)
        body = full.split("<h2>Per-cell economics</h2>", 1)[-1]
        body = "<h2>Per-cell economics</h2>" + body.split("</body>", 1)[0]
        verdict = report.overall_verdict
        vcolour = VERDICT_COLOUR.get(verdict, "#57606a")

    rationale = html.escape(report.rationale) if report else ""
    stale = ""
    if cached.error:
        stale = (f"<div class='warn'>Last report build failed: "
                 f"{html.escape(cached.error)}. Showing the previous "
                 f"result.</div>")

    def stat(label: str, value: Any) -> str:
        shown = "&mdash;" if value is None else html.escape(str(value))
        return (f"<div class='stat'><div class='k'>{label}</div>"
                f"<div class='v'>{shown}</div></div>")

    stats = "".join([
        stat("state", state),
        stat("elapsed (h)", health["elapsed_hours"]),
        stat("quotes", f"{health['proposals_recorded']:,}"
             if health["proposals_recorded"] is not None else None),
        stat("ticks", f"{health['ticks_recorded']:,}"
             if health["ticks_recorded"] is not None else None),
        stat("reconnects", health["reconnects"]),
        stat("on disk", _human_bytes(health["bytes_on_disk"])),
    ])

    built = (time.strftime("%H:%M:%S", time.gmtime(cached.built_at)) + " UTC"
             if cached.built_at else "never")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Census monitor</title>
<meta http-equiv="refresh" content="{refresh_s}">
<style>
 body {{ font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0 auto; max-width: 1100px; padding: 28px 20px; color: #1f2328; }}
 h1 {{ font-size: 20px; margin: 0; }}
 .verdict {{ font-size: 30px; font-weight: 700; color: {vcolour}; margin: 14px 0 4px; }}
 .rationale {{ background: #f6f8fa; border-left: 4px solid {vcolour};
               padding: 10px 14px; border-radius: 4px; margin-bottom: 18px; }}
 .bar {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0 6px; }}
 .stat {{ background: #f6f8fa; border-radius: 6px; padding: 8px 14px; min-width: 92px;
          border-top: 3px solid {colour}; }}
 .stat .k {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
             color: #57606a; }}
 .stat .v {{ font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }}
 .warn {{ background: #fff8c5; border: 1px solid #d4a72c; padding: 8px 12px;
          border-radius: 4px; margin: 10px 0; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ border-bottom: 1px solid #d0d7de; padding: 6px 8px; text-align: left; }}
 th {{ background: #f6f8fa; font-weight: 600; }}
 td.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
 pre {{ background: #f6f8fa; padding: 10px; border-radius: 4px; overflow-x: auto;
        font-size: 12px; }}
 .muted {{ color: #57606a; font-size: 12px; }}
</style></head><body>
<h1>Deriv payout census &mdash; live</h1>
<div class="bar">{stats}</div>
{stale}
<div class="verdict">{html.escape(verdict)}</div>
<div class="rationale">{rationale}</div>
{body}
<p class="muted">Report built {built} in {cached.build_seconds:.1f}s;
rebuilt at most once every {int(DEFAULT_TTL_S)}s. Page refreshes every
{refresh_s}s. Recent events:
{html.escape(', '.join(health['recent_events']) or 'none')}.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "census/1.0"

    def __init__(self, cache: ReportCache, root: Path, refresh_s: int,
                 *args, **kwargs) -> None:
        self._cache = cache
        self._root = root
        self._refresh = refresh_s
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                cached = self._cache.get()
                page = render_page(cached, capture_health(self._root),
                                   self._refresh)
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/health":
                payload = json.dumps(capture_health(self._root), indent=2)
                self._send(200, payload.encode("utf-8"), "application/json")
            elif path == "/api/verdict":
                cached = self._cache.get()
                report = cached.report
                payload = json.dumps({
                    "verdict": report.overall_verdict if report else INSUFFICIENT,
                    "rationale": report.rationale if report else "",
                    "coverage": report.coverage if report else {},
                    "drift": report.drift if report else {},
                    "built_at": cached.built_at,
                    "error": cached.error,
                }, indent=2, default=str)
                self._send(200, payload.encode("utf-8"), "application/json")
            elif path == "/api/cells":
                cached = self._cache.get()
                cells = ([c.as_dict() for c in cached.report.cells]
                         if cached.report else [])
                self._send(200, json.dumps(cells, indent=2,
                                           default=str).encode("utf-8"),
                           "application/json")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 - a bad request must not stop the run
            log.warning("request %s failed: %s", path, exc)
            self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")


def serve(config: CensusConfig, host: str = "127.0.0.1", port: int = 8765,
          refresh_s: int = 30, ttl_s: float = DEFAULT_TTL_S) -> ThreadingHTTPServer:
    cache = ReportCache(config, ttl_s)
    root = Path(config.storage.root)
    handler = partial(Handler, cache, root, refresh_s)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
