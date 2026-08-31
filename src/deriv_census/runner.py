"""Orchestrates the fourteen-day capture.

Two capture streams run concurrently.

**Proposals.** Every quoted payout, obtained by holding a rotating window of
subscribed proposal streams. Subscribing rather than polling is what makes a
fourteen-day run feasible inside a modest rate limit, and it yields the
re-quote sequence for free -- which is how payout drift gets measured.

**Ticks.** A continuous quote stream for a fixed subset of symbols. This is the
input to the settlement-outcome measurement: without it the tie rate can only
be modelled, and the modelled value is not good enough to decide on, because
the tie term is comparable in size to the entire edge being sought.

Tick symbol selection is deliberately fixed for the whole run rather than
rotated. Settlement outcomes are measured over duration-length windows, so a
rotating tick subscription would punch holes in exactly the series that needs
to be continuous. Fewer symbols measured properly beats more measured badly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from . import protocol
from .client import DerivClient, Subscription
from .config import CensusConfig
from .discovery import (Cell, build_cells, resolve_active_symbols,
                        resolve_contracts_for, select_symbols, summarise)
from .protocol import DerivError
from .ratelimit import TokenBucket
from .storage import EVENTS, PROPOSALS, TICKS, CensusStore

log = logging.getLogger(__name__)

#: How long a cell rejected for a transient reason sits out before retry.
COOLDOWN_SECONDS = 900.0


def _batches(items: list[Cell], size: int) -> list[list[Cell]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


@dataclass
class RunnerStats:
    proposals_recorded: int = 0
    ticks_recorded: int = 0
    rotations: int = 0
    cells_permanently_dropped: int = 0
    cells_on_cooldown: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "proposals_recorded": self.proposals_recorded,
            "ticks_recorded": self.ticks_recorded,
            "rotations": self.rotations,
            "cells_permanently_dropped": self.cells_permanently_dropped,
            "cells_on_cooldown": self.cells_on_cooldown,
            "elapsed_hours": round((time.time() - self.started_at) / 3600.0, 3),
        }


class CensusRunner:
    def __init__(self, config: CensusConfig, store: CensusStore) -> None:
        self.config = config
        self.store = store
        self.client = DerivClient(
            config.connection,
            TokenBucket(config.rate_limit.requests_per_minute))
        self.cells: list[Cell] = []
        self.tick_symbols: list[str] = []
        self.stats = RunnerStats()
        self._dropped: set[str] = set()
        self._cooldown: dict[str, float] = {}
        self._tick_subs: dict[str, Subscription] = {}
        self._pip_by_symbol: dict[str, float | None] = {}

    # -- discovery ---------------------------------------------------------

    async def discover(self) -> list[Cell]:
        """Enumerate tradeable cells. Safe to call repeatedly mid-run."""
        grid = self.config.grid
        symbols, probes = await resolve_active_symbols(
            lambda payload: self.client.request(payload, timeout=30.0))
        for probe in probes:
            self.store.event("active_symbols_probe", variant=probe.variant,
                             request=probe.request, count=probe.count,
                             error=probe.error)
        if not symbols:
            log.error("no active_symbols variant returned instruments")
        selected = select_symbols(symbols, grid)
        log.info("discovery: %d symbols returned, %d selected",
                 len(symbols), len(selected))

        cells: list[Cell] = []
        for sym in selected:
            self._pip_by_symbol[sym.symbol] = sym.pip
            offerings, probes = await resolve_contracts_for(
                lambda payload: self.client.request(payload, timeout=30.0),
                sym.symbol, grid.currency)
            if not offerings:
                log.warning("contracts_for %s returned nothing; tried %s",
                            sym.symbol, [p.variant for p in probes])
                self.store.event("contracts_for_failed", symbol=sym.symbol,
                                 probes=[{"variant": p.variant,
                                          "count": p.count,
                                          "error": p.error} for p in probes])
                continue
            cells.extend(build_cells(sym, offerings, grid))

        cells = [c for c in cells if c.key not in self._dropped]
        cells.sort(key=lambda c: (c.symbol, c.duration_seconds, c.contract_type))
        self.cells = cells
        self.store.event("discovery", **summarise(cells),
                         symbols_returned=len(symbols),
                         symbols_selected=len(selected))
        log.info("discovery: %s", summarise(cells))
        return cells

    def choose_tick_symbols(self) -> list[str]:
        """Fixed subset given continuous tick coverage for the whole run."""
        ordered: list[str] = []
        for cell in self.cells:
            if cell.symbol not in ordered:
                ordered.append(cell.symbol)
        chosen = ordered[:self.config.rate_limit.max_concurrent_ticks]
        self.tick_symbols = chosen
        return chosen

    # -- capture -----------------------------------------------------------

    async def _ensure_ticks(self) -> None:
        for symbol in self.tick_symbols:
            sub = self._tick_subs.get(symbol)
            if sub is not None and not sub.closed:
                continue
            try:
                sub = await self.client.subscribe(protocol.ticks(symbol))
                self._tick_subs[symbol] = sub
                log.info("tick stream open: %s", symbol)
            except DerivError as exc:
                log.warning("tick subscribe %s failed: %s", symbol, exc)
                self.store.event("tick_subscribe_failed",
                                 symbol=symbol, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                log.warning("tick subscribe %s failed: %s", symbol, exc)

    def _record_ticks(self) -> None:
        for symbol, sub in list(self._tick_subs.items()):
            for msg in _drain_sync(sub):
                tick = msg.get("tick") or {}
                if not tick:
                    continue
                self.store.write(TICKS, {
                    "symbol": tick.get("symbol", symbol),
                    "tick_epoch": tick.get("epoch"),
                    "quote": tick.get("quote"),
                    "pip_size": tick.get("pip_size",
                                         self._pip_by_symbol.get(symbol)),
                    "conn_epoch": self.client.epoch,
                })
                self.stats.ticks_recorded += 1

    def _record_proposals(self, subs: dict[str, tuple[Cell, Subscription]]) -> None:
        for key, (cell, sub) in subs.items():
            for msg in _drain_sync(sub):
                p = msg.get("proposal") or {}
                if not p:
                    continue
                payout = p.get("payout")
                stake = self.config.grid.stake
                b = ((float(payout) - stake) / stake
                     if isinstance(payout, (int, float)) else None)
                self.store.write(PROPOSALS, {
                    "symbol": cell.symbol,
                    "contract_type": cell.contract_type,
                    "variant": cell.variant,
                    "direction": cell.direction,
                    "duration_s": cell.duration_seconds,
                    "stake": stake,
                    "currency": self.config.grid.currency,
                    "payout": payout,
                    "b": b,
                    "ask_price": p.get("ask_price"),
                    "spot": p.get("spot"),
                    "spot_time": p.get("spot_time"),
                    "date_start": p.get("date_start"),
                    "conn_epoch": self.client.epoch,
                    "sub_id": sub.subscription_id,
                })
                self.stats.proposals_recorded += 1

    def _eligible(self, cells: list[Cell]) -> list[Cell]:
        now = time.monotonic()
        expired = [k for k, until in self._cooldown.items() if until <= now]
        for key in expired:
            self._cooldown.pop(key, None)
        self.stats.cells_on_cooldown = len(self._cooldown)
        return [c for c in cells
                if c.key not in self._dropped and c.key not in self._cooldown]

    def _handle_cell_error(self, cell: Cell, exc: Exception) -> None:
        if isinstance(exc, DerivError) and exc.is_permanent:
            self._dropped.add(cell.key)
            self.stats.cells_permanently_dropped += 1
            self.store.event("cell_dropped", cell=cell.key, error=str(exc))
            log.info("dropping %s permanently: %s", cell.key, exc)
        else:
            self._cooldown[cell.key] = time.monotonic() + COOLDOWN_SECONDS
            self.store.event("cell_cooldown", cell=cell.key, error=str(exc))
            log.debug("cooling down %s: %s", cell.key, exc)

    async def _rotate_once(self, batch: list[Cell]) -> None:
        """Subscribe one batch, dwell while recording, then release."""
        subs: dict[str, tuple[Cell, Subscription]] = {}
        for cell in batch:
            duration, unit = cell.duration_request
            request = protocol.proposal(
                symbol=cell.symbol, contract_type=cell.contract_type,
                duration=duration, duration_unit=unit,
                stake=self.config.grid.stake,
                currency=self.config.grid.currency, subscribe=True)
            try:
                subs[cell.key] = (cell, await self.client.subscribe(request))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._handle_cell_error(cell, exc)

        if not subs:
            return

        deadline = time.monotonic() + self.config.sampling.dwell_seconds
        try:
            while time.monotonic() < deadline:
                self._record_proposals(subs)
                self._record_ticks()
                await asyncio.sleep(1.0)
            self._record_proposals(subs)
            self._record_ticks()
        finally:
            for _cell, sub in subs.values():
                with contextlib.suppress(Exception):
                    await self.client.unsubscribe(sub)
        self.stats.rotations += 1

    # -- main loop ---------------------------------------------------------

    async def run(self, deadline_epoch: float | None = None) -> RunnerStats:
        cfg = self.config
        if deadline_epoch is None:
            deadline_epoch = time.time() + cfg.sampling.duration_days * 86400.0

        await self.client.connect()
        self.store.event("run_started",
                         endpoint=cfg.connection.endpoint,
                         config=cfg.to_dict(),
                         deadline_epoch=deadline_epoch)
        try:
            await self.discover()
            self.choose_tick_symbols()
            last_discovery = time.monotonic()

            while time.time() < deadline_epoch:
                if not self.client.connected:
                    await self.client.connect()
                epoch = self.client.epoch
                await self._ensure_ticks()

                eligible = self._eligible(self.cells)
                if not eligible:
                    log.warning("no eligible cells; waiting")
                    self.store.event("no_eligible_cells")
                    await asyncio.sleep(60.0)
                    continue

                for batch in _batches(eligible,
                                      cfg.rate_limit.max_concurrent_proposals):
                    if time.time() >= deadline_epoch:
                        break
                    if self.client.epoch != epoch:
                        # Reconnected: subscriptions are gone. Restart the
                        # cycle so ticks are re-established before sampling.
                        log.info("connection epoch changed; restarting cycle")
                        self.store.event("epoch_changed",
                                         old=epoch, new=self.client.epoch)
                        self._tick_subs.clear()
                        break
                    await self._rotate_once(batch)
                    await asyncio.sleep(cfg.sampling.rotation_pause_seconds)

                elapsed_min = (time.monotonic() - last_discovery) / 60.0
                if elapsed_min >= cfg.sampling.rediscover_every_minutes:
                    await self.discover()
                    self.choose_tick_symbols()
                    last_discovery = time.monotonic()

                self.store.event("heartbeat", **self.stats.as_dict(),
                                 client=vars(self.client.stats))
        finally:
            self.store.event("run_finished", **self.stats.as_dict(),
                             client=vars(self.client.stats))
            with contextlib.suppress(Exception):
                await self.client.forget_all("proposal", "ticks")
            await self.client.close()
            self.store.flush()
        return self.stats


def _drain_sync(sub: Subscription) -> list[dict]:
    """Non-blocking drain of a subscription queue."""
    out: list[dict] = []
    while True:
        try:
            item = sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            return out
        if item is None:
            sub.closed = True
            return out
        out.append(item)
