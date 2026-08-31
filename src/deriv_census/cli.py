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
from .discovery import (build_cells, resolve_active_symbols,
                        resolve_contracts_for, select_symbols)
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


class _PreflightStop(Exception):
    """Ends preflight early while still reaching the teardown and verdict.

    Several checks are terminal -- a closed market, no instruments, a venue
    that does not serve this location. Returning directly from inside the
    body skipped the closing verdict, so a run could end having printed a
    diagnosis but never the sentence that says what it means. Raising instead
    keeps one exit path: teardown, capture written, verdict printed.
    """

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class Preflight:
    """Records checks and prints them as they happen.

    Two kinds. A gating check decides whether a capture may start. An
    advisory one is an observation on the way there -- which of several
    request shapes a venue accepted, what country it resolved. Advisory
    results are printed but do not fail the run: probing five shapes and
    using the one that works is success, not five failures, and counting
    them as failures would block a capture that is perfectly able to
    proceed.
    """

    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []
        self.notes: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "",
               advisory: bool = False) -> None:
        (self.notes if advisory else self.checks).append((name, ok, detail))
        marker = "info" if advisory else ("PASS" if ok else "FAIL")
        print(f"  [{marker}] {name}" + (f"\n         {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


async def diagnose_empty_offerings(pf: "Preflight", probe_request
                                   ) -> tuple[str | None, bool]:
    """Establish why no instruments were returned.

    Deriv scopes offerings by jurisdiction, so an empty list under every
    request shape most likely means the venue serves nothing to this
    connection's country. ``website_status`` reports the country Deriv
    resolved, and ``landing_company`` reports which entities -- if any --
    cover it. Both are unauthenticated.
    """
    country: str | None = None
    served = True
    try:
        payload = await probe_request("website_status", protocol.website_status(),
                                      timeout=30)
        status = payload.get("website_status") or {}
        country = status.get("clients_country")
        pf.record("website_status", bool(status),
                  f"Deriv resolves this connection to country={country!r}, "
                  f"site_status={status.get('site_status')!r}", advisory=True)
    except Exception as exc:  # noqa: BLE001 - diagnosis must not raise
        pf.record("website_status", False, str(exc), advisory=True)

    if not country:
        return None, served

    try:
        payload = await probe_request(f"landing_company:{country}",
                                      protocol.landing_company(country),
                                      timeout=30)
        block = payload.get("landing_company") or {}
        entities = {key: value.get("shortcode")
                    for key, value in block.items()
                    if isinstance(value, dict) and value.get("shortcode")}
        served = bool(entities)
        pf.record(f"landing_company for {country!r}", served,
                  f"entities: {entities}" if entities else
                  "no entity serves this country -- Deriv may not offer these "
                  "products from here, which no request tuning can change",
                  advisory=True)
    except Exception as exc:  # noqa: BLE001
        pf.record(f"landing_company for {country!r}", False, str(exc),
                  advisory=True)
    return country, served


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
    stop_code: int | None = None
    # Bound before the body so the closing summary is safe on every exit
    # path, including the terminal checks that stop early.
    quotes: dict[str, float] = {}

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
        def symbols_label(payload: dict) -> str:
            extras = "+".join(sorted(set(payload) - {"active_symbols"}))
            base = f"active_symbols:{payload.get('active_symbols')}"
            return f"{base}+{extras}" if extras else base

        symbols, probes = await resolve_active_symbols(
            lambda payload: probe_request(symbols_label(payload), payload,
                                          timeout=30))
        for probe in probes:
            pf.record(f"active_symbols variant '{probe.variant}'", probe.worked,
                      (f"{probe.count} symbols" if probe.error is None
                       else f"error: {probe.error}")
                      + f"   request={probe.request}", advisory=True)
        if not symbols:
            # Every request shape returning an empty list is not a malformed
            # request -- it is far more likely that nothing is offered to this
            # connection's jurisdiction. Establish which before giving up.
            country, served = await diagnose_empty_offerings(pf, probe_request)
            if not served:
                print(venue_unavailable_summary(country))
                pf.record("venue serves this location", False,
                          f"Deriv lists no entity for country {country!r}")
                raise _PreflightStop(1)
            print("\n  Discovery returned no instruments under any request "
                  "shape.\n  Falling back to asking for a price on the major "
                  "FX pairs directly:\n  a quote is the measurement anyway, "
                  "and a refusal carries a reason code.\n")
            symbols = [protocol.SymbolInfo(code, code, "forex", "major_pairs",
                                           True, False, None)
                       for code in protocol.FALLBACK_FX_SYMBOLS]
            pf.record("instruments resolved", True,
                      f"{len(symbols)} major FX pairs assumed by fallback; "
                      "the quote below is what proves them")
        else:
            pf.record("instruments resolved", True,
                      f"{len(symbols)} returned by discovery")

        selected = select_symbols(symbols, cfg.grid)
        open_syms = [s for s in selected if s.tradeable]
        pf.record("grid symbols after filtering", bool(selected),
                  f"{len(selected)} selected, {len(open_syms)} currently open")
        if not selected:
            raise _PreflightStop(1)

        probe = (open_syms or selected)[0]
        pf.record("pip size from discovery", probe.pip is not None,
                  f"{probe.symbol} pip={probe.pip} decimals={probe.pip_decimals}",
                  advisory=True)

        # --- contract availability -----------------------------------------
        offerings, cf_probes = await resolve_contracts_for(
            lambda payload: probe_request(
                f"contracts_for:{probe.symbol}"
                f"{':product_type' if 'product_type' in payload else ''}",
                payload, timeout=30),
            probe.symbol, cfg.grid.currency)
        for cf in cf_probes:
            pf.record(f"contracts_for shape '{cf.variant}'", cf.worked,
                      f"{cf.count} offerings" if cf.error is None
                      else f"error: {cf.error}", advisory=True)
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
            raise _PreflightStop(1)

        # --- the measurement itself -----------------------------------------
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
        try:
            sub = await client.subscribe(protocol.ticks(probe.symbol))
        except DerivError as exc:
            pf.record("tick stream", False, f"{probe.symbol}: {exc}")
            raise _PreflightStop(1) from exc
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

        # Ties are decided by comparing quotes at the feed's own precision, so
        # a pip size must be available from somewhere. The tick carries it,
        # which is also where the analysis reads it, so discovery not
        # supplying one is not a blocker.
        tick_pip = next((t.get("pip_size") for t in received
                         if t.get("pip_size")), None)
        pip = probe.pip or tick_pip
        pf.record("pip size available", pip is not None,
                  f"pip={pip} (from {'discovery' if probe.pip else 'tick stream'})"
                  "  -- needed to compare quotes exactly when measuring ties"
                  if pip else
                  "no pip size from discovery or ticks; tie measurement would "
                  "be unreliable")

    except _PreflightStop as stop:
        stop_code = stop.code
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
                "notes": [{"name": n, "ok": ok, "detail": d}
                          for n, ok, d in pf.notes],
                "exchanges": raw,
            }, indent=2, default=str), encoding="utf-8")
            print(f"\nwrote raw API capture to {path} "
                  f"({len(raw)} exchanges)")

    print()
    if quotes:
        print(plain_english_summary(quotes, cfg.grid.durations_seconds[0]))

    print()
    if pf.ok and stop_code in (None, 0):
        print("PREFLIGHT PASSED. The wire format matches what the census "
              "expects; a run will record valid data.")
    else:
        failed = [n for n, ok, _ in pf.checks if not ok]
        print("PREFLIGHT FAILED: " + ", ".join(failed))
        if any("venue serves this location" in name for name in failed):
            print("This is a venue availability finding, not a fault to fix.")
        else:
            print("Do not start a 14-day run until these pass.")
    print()
    if stop_code is not None:
        return stop_code
    return 0 if pf.ok else 1


def venue_unavailable_summary(country: str | None) -> str:
    """State the jurisdiction finding plainly. It is a result, not a fault.

    Deriv lists no entity covering the country the connection resolved to, so
    every symbol is invalid and no request, parameter or credential changes
    that. Reporting it as a configuration problem would send someone hunting
    for a fix that does not exist.
    """
    where = f"'{country}'" if country else "this location"
    return "\n".join([
        "=" * 68,
        "WHAT THIS MEANS",
        "=" * 68,
        "",
        f"  Deriv sees this connection as coming from {where}, and lists no",
        "  company of its own that serves it.",
        "",
        "  That is why every symbol came back invalid. Deriv is not offering",
        "  these products to this location at all - so there is no payout to",
        "  measure, and no configuration change that would reveal one.",
        "",
        "  This is a real answer, not a failure. It settles the venue",
        "  question that has been open since the start:",
        "",
        "    - The measurement cannot be taken here, because there is",
        "      nothing on offer to measure.",
        "    - No amount of model quality changes an unavailable venue.",
        "",
        "  What it does NOT tell you is whether the economics would have",
        "  worked somewhere Deriv does operate. That question is still open,",
        "  and still worth answering before building anything.",
        "=" * 68,
    ])


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
