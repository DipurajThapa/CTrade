"""An in-process fake of the Deriv WebSocket API.

Enough of the real protocol to exercise the whole census end to end: request
correlation by ``req_id``, streaming subscriptions, ``forget``, error objects,
and a tick feed quantised to a pip grid so the tie measurement has something
real to find.

The point is that every test below runs the actual client, the actual runner
and the actual analysis against a socket, rather than against mocks that would
happily agree with whatever the code does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import time
from dataclasses import dataclass, field

from websockets.asyncio.server import serve

PIP = 1e-5
DECIMALS = 5


@dataclass
class FakeConfig:
    #: Payout fraction the fake quotes, before jitter.
    payout: float = 0.92
    payout_jitter: float = 0.01
    #: Per-tick volatility in price units; small values make ties common.
    tick_sigma: float = 8e-6
    tick_interval_s: float = 0.02
    proposal_interval_s: float = 0.05
    symbols: list[str] = field(default_factory=lambda: ["frxEURUSD", "frxGBPUSD"])
    #: Symbols that should be reported as closed.
    closed: set[str] = field(default_factory=set)
    #: Contract types offered.
    contract_types: list[str] = field(
        default_factory=lambda: ["CALL", "PUT", "CALLE", "PUTE"])
    #: Fail these contract types with a permanent error.
    reject_types: set[str] = field(default_factory=set)
    fail_every_nth_proposal: int = 0
    #: When set, active_symbols returns an EMPTY list unless the request
    #: contains every one of these key/value pairs. Reproduces the live
    #: failure where one request shape returns nothing while another works.
    active_symbols_requires: dict | None = None
    #: Advance tick epochs by this many seconds each tick instead of using the
    #: wall clock. Lets a six-second test produce hours of tick history, so the
    #: settlement measurement has something real to chew on.
    tick_epoch_step: int = 1


class FakeDerivServer:
    def __init__(self, config: FakeConfig | None = None, seed: int = 11) -> None:
        self.config = config or FakeConfig()
        self.rng = random.Random(seed)
        self.server = None
        self.port = 0
        self.request_counts: dict[str, int] = {}
        self._sub_seq = 0
        self._proposal_seq = 0
        self._prices = {s: 1.10000 for s in self.config.symbols}
        self._tick_epoch = int(time.time())

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "FakeDerivServer":
        self.server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    # -- price process -----------------------------------------------------

    def _next_epoch(self) -> int:
        if self.config.tick_epoch_step:
            self._tick_epoch += self.config.tick_epoch_step
            return self._tick_epoch
        return int(time.time())

    def _next_quote(self, symbol: str) -> float:
        price = self._prices.get(symbol, 1.10000)
        price += self.rng.gauss(0.0, self.config.tick_sigma)
        price = round(price, DECIMALS)
        self._prices[symbol] = price
        return price

    def _payout(self, stake: float) -> float:
        b = self.config.payout + self.rng.uniform(
            -self.config.payout_jitter, self.config.payout_jitter)
        return round(stake * (1.0 + max(b, 0.01)), 2)

    # -- protocol ----------------------------------------------------------

    async def _handle(self, ws) -> None:
        tasks: dict[str, asyncio.Task] = {}
        try:
            async for raw in ws:
                msg = json.loads(raw)
                req_id = msg.get("req_id")
                await self._route(ws, msg, req_id, tasks)
        except Exception:  # noqa: BLE001 - client disconnects are normal
            pass
        finally:
            for task in tasks.values():
                task.cancel()
            for task in tasks.values():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _route(self, ws, msg, req_id, tasks) -> None:
        cfg = self.config

        def count(kind: str) -> None:
            self.request_counts[kind] = self.request_counts.get(kind, 0) + 1

        async def send(payload: dict) -> None:
            payload["echo_req"] = msg
            if req_id is not None:
                payload["req_id"] = req_id
            await ws.send(json.dumps(payload))

        async def error(code: str, message: str) -> None:
            await send({"msg_type": "error",
                        "error": {"code": code, "message": message}})

        if "ping" in msg:
            count("ping")
            await send({"msg_type": "ping", "ping": "pong"})

        elif "active_symbols" in msg:
            count("active_symbols")
            required = cfg.active_symbols_requires
            if required and any(msg.get(k) != v for k, v in required.items()):
                await send({"msg_type": "active_symbols", "active_symbols": []})
                return
            await send({"msg_type": "active_symbols", "active_symbols": [
                {"symbol": s, "display_name": s.replace("frx", ""),
                 "market": "forex", "submarket": "major_pairs",
                 "exchange_is_open": 0 if s in cfg.closed else 1,
                 "is_trading_suspended": 0, "pip": PIP}
                for s in cfg.symbols
            ] + [
                {"symbol": "R_100", "display_name": "Volatility 100 Index",
                 "market": "synthetic_index", "submarket": "random_index",
                 "exchange_is_open": 1, "is_trading_suspended": 0, "pip": 0.01}
            ]})

        elif "contracts_for" in msg:
            count("contracts_for")
            await send({"msg_type": "contracts_for", "contracts_for": {
                "available": [
                    {"contract_type": ct, "contract_category": "callput",
                     "min_contract_duration": "15s",
                     "max_contract_duration": "365d",
                     "barrier_category": "euro_atm", "start_type": "spot"}
                    for ct in cfg.contract_types]}})

        elif "proposal" in msg:
            count("proposal")
            contract_type = msg.get("contract_type")
            if contract_type in cfg.reject_types:
                await error("ContractValidationError",
                            f"{contract_type} not offered")
                return
            self._proposal_seq += 1
            if (cfg.fail_every_nth_proposal
                    and self._proposal_seq % cfg.fail_every_nth_proposal == 0):
                await error("RateLimit", "You are rate limited")
                return
            symbol = msg.get("symbol", cfg.symbols[0])
            if symbol in cfg.closed:
                await error("MarketIsClosed", "This market is presently closed")
                return
            stake = float(msg.get("amount", 10.0))

            def build() -> dict:
                return {"msg_type": "proposal", "proposal": {
                    "id": f"prop-{self._sub_seq}",
                    "payout": self._payout(stake),
                    "ask_price": stake,
                    "spot": self._next_quote(symbol),
                    "spot_time": int(time.time()),
                    "date_start": int(time.time()),
                    "longcode": f"Win payout if {symbol} is strictly higher"}}

            if msg.get("subscribe"):
                self._sub_seq += 1
                sub_id = f"sub-proposal-{self._sub_seq}"
                first = build()
                first["subscription"] = {"id": sub_id}
                await send(first)
                tasks[sub_id] = asyncio.create_task(
                    self._stream(ws, msg, req_id, sub_id, build,
                                 cfg.proposal_interval_s))
            else:
                await send(build())

        elif "ticks" in msg:
            count("ticks")
            symbol = msg["ticks"]
            self._sub_seq += 1
            sub_id = f"sub-ticks-{self._sub_seq}"

            def build_tick() -> dict:
                return {"msg_type": "tick", "tick": {
                    "symbol": symbol, "epoch": self._next_epoch(),
                    "quote": self._next_quote(symbol), "pip_size": PIP,
                    "id": sub_id}}

            first = build_tick()
            first["subscription"] = {"id": sub_id}
            await send(first)
            tasks[sub_id] = asyncio.create_task(
                self._stream(ws, msg, req_id, sub_id, build_tick,
                             cfg.tick_interval_s))

        elif "forget" in msg:
            count("forget")
            task = tasks.pop(msg["forget"], None)
            if task is not None:
                task.cancel()
            await send({"msg_type": "forget", "forget": 1})

        elif "forget_all" in msg:
            count("forget_all")
            for task in tasks.values():
                task.cancel()
            tasks.clear()
            await send({"msg_type": "forget_all", "forget_all": []})

        else:
            await error("UnrecognisedRequest", f"unknown: {sorted(msg)[:3]}")

    async def _stream(self, ws, request, req_id, sub_id, build, interval) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                payload = build()
                payload["subscription"] = {"id": sub_id}
                payload["echo_req"] = request
                if req_id is not None:
                    payload["req_id"] = req_id
                await ws.send(json.dumps(payload))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - client gone
            return
