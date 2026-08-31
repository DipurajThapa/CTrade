"""Command line entry point.

    census preflight              verify the live API before committing a run
    census run                    the capture itself
    census serve                  local dashboard for watching a capture
    census analyse                compute the verdict from captured data
    census export                 write Parquet copies of the raw streams

``preflight`` exists because a fourteen-day run that discovers on day fourteen
that a field name was wrong is fourteen days lost. It checks every assumption
this package makes about the wire format, and reports each as PASS or FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

from . import protocol
from . import __version__
from .analysis import build_report
from .client import DerivClient
from .config import CensusConfig, load_config
from .discovery import build_cells, select_symbols
from .protocol import DerivError
from .ratelimit import TokenBucket
from .report import render_text, write_html
from .runner import CensusRunner
from .storage import CensusStore, export_parquet

log = logging.getLogger("census")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class Preflight:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"\n         {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


async def run_preflight(cfg: CensusConfig, dump_raw: str | None = None) -> int:
    """Validate every wire-format assumption against the live API.

    ``dump_raw`` writes each request/response pair verbatim to a JSON file.
    That file is the evidence: it lets the parsing in ``protocol.py`` be
    checked against real Deriv responses by someone who cannot reach the API
    themselves. It contains only public market data and the application id --
    no account state, no credentials, because this client has no
    authentication path to produce any.
    """
    pf = Preflight()
    print(f"\nPreflight against {cfg.connection.url()}\n")

    raw: list[dict] = []

    client = DerivClient(cfg.connection,
                         TokenBucket(cfg.rate_limit.requests_per_minute))
    try:
        # Bounded so an unreachable or blocked endpoint reports promptly
        # instead of retrying silently behind the backoff.
        await asyncio.wait_for(client.connect(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        pf.record("websocket connect", False, str(exc))
        return 1
    pf.record("websocket connect", True, cfg.connection.endpoint)

    async def probe_request(label: str, payload: dict, **kwargs) -> dict:
        """Issue a request, recording the exchange whether it succeeds or not.

        Failures are recorded too: an error response is exactly the evidence
        needed to work out why a check failed.
        """
        try:
            response = await client.request(payload, **kwargs)
        except DerivError as exc:
            raw.append({"label": label, "request": payload,
                        "error": {"code": exc.code, "message": exc.message}})
            raise
        raw.append({"label": label, "request": payload, "response": response})
        return response

    try:
        await probe_request("ping", protocol.ping())
        pf.record("ping", True)

        # --- discovery -----------------------------------------------------
        payload = await probe_request("active_symbols",
                                      protocol.active_symbols(), timeout=30)
        symbols = protocol.parse_active_symbols(payload)
        pf.record("active_symbols", bool(symbols),
                  f"{len(symbols)} symbols returned")

        selected = select_symbols(symbols, cfg.grid)
        open_syms = [s for s in selected if s.tradeable]
        pf.record("grid symbols after filtering", bool(selected),
                  f"{len(selected)} selected, {len(open_syms)} currently open")
        if not selected:
            return 1

        probe = (open_syms or selected)[0]
        pf.record("pip size present", probe.pip is not None,
                  f"{probe.symbol} pip={probe.pip} "
                  f"decimals={probe.pip_decimals}  "
                  "(required to compare quotes for ties)")

        # --- contract availability -----------------------------------------
        payload = await probe_request(
            f"contracts_for:{probe.symbol}",
            protocol.contracts_for(probe.symbol, cfg.grid.currency), timeout=30)
        offerings = protocol.parse_contracts_for(payload)
        available = {o.contract_type for o in offerings}
        for variant in cfg.grid.variants:
            rise, fall = protocol.CONTRACT_TYPES[variant]
            pf.record(f"contract type {rise}/{fall} ({variant}) offered",
                      rise in available,
                      f"{probe.symbol} offers {len(available)} contract types")

        cells = build_cells(probe, offerings, cfg.grid)
        pf.record("grid cells constructible", bool(cells),
                  f"{len(cells)} cells for {probe.symbol} "
                  f"at durations {cfg.grid.durations_seconds}")

        if not probe.tradeable:
            pf.record("market open for quoting", False,
                      f"{probe.symbol} is closed; re-run preflight during "
                      "market hours to validate quoting")
            return 1 if not pf.ok else 0

        # --- the measurement itself -----------------------------------------
        quotes: dict[str, float] = {}
        duration = cfg.grid.durations_seconds[0]
        for variant in cfg.grid.variants:
            rise, fall = protocol.CONTRACT_TYPES[variant]
            for contract_type in (rise, fall):
                if contract_type not in available:
                    continue
                try:
                    payload = await probe_request(
                        f"proposal:{contract_type}:{duration}s",
                        protocol.proposal(
                            probe.symbol, contract_type,
                            duration // 60 if duration % 60 == 0 else duration,
                            "m" if duration % 60 == 0 else "s",
                            cfg.grid.stake, cfg.grid.currency, subscribe=False),
                        timeout=30)
                except DerivError as exc:
                    pf.record(f"proposal {contract_type}", False, str(exc))
                    continue
                p = payload.get("proposal") or {}
                payout = p.get("payout")
                if not isinstance(payout, (int, float)):
                    pf.record(f"proposal {contract_type}", False,
                              f"no numeric payout in response: "
                              f"{sorted(p)[:8]}")
                    continue
                b = (float(payout) - cfg.grid.stake) / cfg.grid.stake
                quotes[contract_type] = b
                p_be = 1.0 / (1.0 + b)
                pf.record(f"proposal {contract_type} @{duration}s", True,
                          f"stake {cfg.grid.stake} -> payout {payout} "
                          f"=> b={b:.4f}, break-even={p_be:.2%}, "
                          f"house margin={p_be - 0.5:.2%}")

        # The grid quotes Rise only, on the assumption Deriv prices Rise and
        # Fall symmetrically. Verify rather than assume: if they differ, the
        # config must sample both directions or the census is half blind.
        for variant in cfg.grid.variants:
            rise, fall = protocol.CONTRACT_TYPES[variant]
            if rise in quotes and fall in quotes:
                gap = abs(quotes[rise] - quotes[fall])
                pf.record(f"{variant} rise/fall payout symmetry", gap < 0.01,
                          f"b({rise})={quotes[rise]:.4f} vs "
                          f"b({fall})={quotes[fall]:.4f}, gap={gap:.4f}"
                          + ("" if gap < 0.01 else
                             "  -> set grid.directions to ['rise','fall']"))

        # --- streaming -------------------------------------------------------
        sub = await client.subscribe(protocol.ticks(probe.symbol))
        received, deadline = [], time.monotonic() + 15
        while len(received) < 3 and time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(sub.queue.get(), timeout=5)
            except asyncio.TimeoutError:
                break
            if msg and msg.get("tick"):
                received.append(msg["tick"])
                raw.append({"label": "tick", "request": protocol.ticks(probe.symbol),
                            "response": msg})
        await client.unsubscribe(sub)
        pf.record("tick stream", len(received) >= 2,
                  f"{len(received)} ticks in 15s; "
                  f"sample={received[0] if received else 'none'}")

    finally:
        await client.close()
        if dump_raw:
            path = Path(dump_raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
                "endpoint": cfg.connection.endpoint,
                "app_id": cfg.connection.app_id,
                "package_version": __version__,
                "checks": [{"name": n, "passed": ok, "detail": d}
                           for n, ok, d in pf.checks],
                "exchanges": raw,
            }, indent=2, default=str), encoding="utf-8")
            print(f"\nwrote raw API capture to {path} "
                  f"({len(raw)} exchanges)")

    print()
    if quotes:
        print(plain_english_summary(quotes, cfg.grid.durations_seconds[0]))

    print()
    if pf.ok:
        print("PREFLIGHT PASSED. The wire format matches what the census "
              "expects; a run will record valid data.")
    else:
        failed = [n for n, ok, _ in pf.checks if not ok]
        print("PREFLIGHT FAILED: " + ", ".join(failed))
        print("Do not start a 14-day run until these pass.")
    print()
    return 0 if pf.ok else 1


def plain_english_summary(quotes: dict[str, float], duration_s: int) -> str:
    """Say what the measured payout means, without requiring any statistics.

    Preflight is one snapshot, not the verdict -- it cannot see how the payout
    varies, and it has no tie-rate measurement. But a snapshot is enough to
    rule the venue out: if the break-even win rate is already far beyond what
    a good model reaches, fourteen days of measurement will not rescue it.
    """
    import statistics

    b = statistics.median(quotes.values())
    p_be = 1.0 / (1.0 + b)
    cut = p_be - 0.5

    lines = [
        "=" * 68,
        "WHAT THIS MEANS",
        "=" * 68,
        "",
        f"  On a $10 trade at {duration_s // 60 or duration_s} "
        f"{'minute' if duration_s >= 60 else 'second'}"
        f"{'s' if (duration_s // 60 or duration_s) != 1 else ''}, "
        f"Deriv pays ${10 * (1 + b):.2f} if you win",
        f"  and keeps your $10 if you lose.",
        "",
        f"  So to break even you must be right {p_be:.1%} of the time.",
        f"  A coin flip is 50%. Deriv's cut is the {cut:.1%} difference.",
        "",
    ]

    # An out-of-sample information coefficient of 0.03-0.08 on short-horizon
    # FX -- a good result, honestly measured -- is a win rate of 51.2-53.2%.
    if p_be <= 0.515:
        lines += [
            "  A very good prediction system reaches about 51-53%.",
            f"  {p_be:.1%} is inside that range.",
            "",
            "  VERDICT: Worth measuring properly. Run the 14-day capture.",
        ]
    elif p_be <= 0.532:
        lines += [
            "  A very good prediction system reaches about 51-53%.",
            f"  {p_be:.1%} is at the top of that range - possible, but it",
            "  needs your system to be genuinely world class.",
            "",
            "  VERDICT: Borderline. The 14-day capture is worth running,",
            "  because the payout moves and some hours may be better.",
        ]
    else:
        lines += [
            "  A very good prediction system reaches about 51-53%.",
            f"  {p_be:.1%} is beyond that. Not difficult - beyond.",
            "",
            "  VERDICT: Deriv's cut is too big here. No prediction system,",
            "  however good, makes money at this payout. This is the",
            "  answer you were looking for, and it cost you 30 seconds.",
        ]

    lines += [
        "",
        "  Caveat: this is ONE quote, right now. The payout moves through",
        "  the day and the 14-day capture measures that properly. But a",
        "  number far outside the range above will not be rescued by it.",
        "=" * 68,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run / analyse / export
# ---------------------------------------------------------------------------


async def run_capture(cfg: CensusConfig, days: float | None) -> int:
    if days is not None:
        cfg.sampling.duration_days = days
    root = Path(cfg.storage.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config-used.json").write_text(
        json.dumps(cfg.to_dict(), indent=2, default=str), encoding="utf-8")

    store = CensusStore(root, cfg.storage.flush_interval_s)
    runner = CensusRunner(cfg, store)

    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("shutdown signal received; finishing current rotation")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    deadline = time.time() + cfg.sampling.duration_days * 86400.0
    task = asyncio.create_task(runner.run(deadline_epoch=deadline))
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({task, waiter},
                                 return_when=asyncio.FIRST_COMPLETED)
    if task not in done:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    waiter.cancel()

    try:
        store.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("store close failed: %s", exc)
    log.info("capture finished: %s", runner.stats.as_dict())
    return 0


def run_analysis(cfg: CensusConfig, html_out: str | None) -> int:
    report = build_report(cfg.storage.root, cfg.decision)
    print(render_text(report))

    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = report.to_frame()
    if not frame.empty:
        frame.to_csv(out_dir / "cells.csv", index=False)
        print(f"wrote {out_dir / 'cells.csv'}")
    (out_dir / "verdict.json").write_text(json.dumps({
        "generated_at": report.generated_at,
        "verdict": report.overall_verdict,
        "rationale": report.rationale,
        "coverage": report.coverage,
        "drift": report.drift,
    }, indent=2), encoding="utf-8")
    print(f"wrote {out_dir / 'verdict.json'}")
    if html_out:
        print(f"wrote {write_html(report, html_out)}")
    return 0


def run_serve(cfg: CensusConfig, host: str, port: int, refresh: int) -> int:
    from .server import serve

    httpd = serve(cfg, host=host, port=port, refresh_s=refresh)
    actual_host, actual_port = httpd.server_address[:2]
    print(f"\nCensus monitor on http://{actual_host}:{actual_port}")
    print(f"  reading  {Path(cfg.storage.root).resolve()}")
    print(f"  JSON at  /api/health  /api/verdict  /api/cells")
    print("  Ctrl-C to stop. This does not affect a running capture.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def run_export(cfg: CensusConfig) -> int:
    out = Path("reports")
    for stream in ("proposals", "ticks", "events"):
        rows = export_parquet(cfg.storage.root, stream,
                              out / f"{stream}.parquet")
        print(f"{stream}: {rows:,} rows -> {out / f'{stream}.parquet'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="census",
        description="Measure Deriv's payout and settlement-tie distribution "
                    "to decide whether a short-horizon binary strategy is "
                    "mathematically viable before any capital is committed.")
    parser.add_argument("-c", "--config", default="config/census.yaml",
                        help="YAML config path (default: config/census.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="verify the live API before a run")
    pre.add_argument("--dump-raw", default=None, metavar="PATH",
                     help="write every request/response pair verbatim to a "
                          "JSON file, as evidence that the wire format "
                          "matches what this package parses")
    run_cmd = sub.add_parser("run", help="capture payout and tick data")
    run_cmd.add_argument("--days", type=float, default=None,
                         help="override the configured capture duration")
    srv = sub.add_parser("serve", help="local dashboard for watching a capture")
    srv.add_argument("--host", default="127.0.0.1",
                     help="bind address (default: localhost only)")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--refresh", type=int, default=30,
                     help="page auto-refresh interval in seconds")

    an = sub.add_parser("analyse", aliases=["analyze"],
                        help="compute the verdict from captured data")
    an.add_argument("--html", default="reports/census.html",
                    help="path for the HTML report ('' to skip)")
    sub.add_parser("export", help="write Parquet copies of the raw streams")

    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    config_path = Path(args.config)
    cfg = load_config(config_path if config_path.exists() else None)
    if not config_path.exists():
        log.warning("config %s not found; using defaults", config_path)

    if args.command == "preflight":
        return asyncio.run(run_preflight(cfg, args.dump_raw))
    if args.command == "run":
        return asyncio.run(run_capture(cfg, args.days))
    if args.command == "serve":
        return run_serve(cfg, args.host, args.port, args.refresh)
    if args.command in ("analyse", "analyze"):
        return run_analysis(cfg, args.html or None)
    if args.command == "export":
        return run_export(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
