"""Resilient async client for the Deriv WebSocket API.

Designed for a fourteen-day unattended run, so the failure modes that matter
are the slow ones: a silently dead socket, a subscription the server has
forgotten, a reconnect storm, an unbounded queue after a consumer stalls.

Design notes
------------
* Every request carries a ``req_id``; responses are dispatched by it rather
  than by arrival order, so a slow ``contracts_for`` cannot be mistaken for a
  fast ``proposal``.
* Subscriptions deliver into bounded queues. If a consumer stalls, the oldest
  update is dropped and counted rather than growing memory without limit --
  losing a stale quote is always preferable to losing the run.
* Reconnection is the caller's cue to re-establish subscriptions. The client
  deliberately does NOT silently resubscribe: a resubscribe that quietly fails
  would leave the run recording nothing while appearing healthy. Instead the
  connection epoch increments and consumers see their streams close.
* The client has no authentication path. It cannot send a token and therefore
  cannot trade, whatever the configuration says.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection, connect

from . import protocol
from .config import ConnectionConfig
from .protocol import DerivError
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

#: Bounded per-subscription buffer. Deep enough to ride out a slow analysis
#: step, shallow enough that a wedged consumer cannot exhaust memory.
QUEUE_MAXSIZE = 512


class ConnectionLost(RuntimeError):
    """The socket dropped while a request or subscription was outstanding."""


@dataclass
class Subscription:
    req_id: int
    request: dict[str, Any]
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAXSIZE))
    subscription_id: str | None = None
    dropped: int = 0
    closed: bool = False

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


@dataclass
class ClientStats:
    messages_received: int = 0
    messages_sent: int = 0
    errors: int = 0
    rate_limit_errors: int = 0
    reconnects: int = 0
    dropped_updates: int = 0


class DerivClient:
    def __init__(self, config: ConnectionConfig, bucket: TokenBucket) -> None:
        self._config = config
        self._bucket = bucket
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task | None = None
        self._pinger: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[int, Subscription] = {}
        self._next_req_id = 1
        self._epoch = 0
        self._closing = False
        self.stats = ClientStats()

    # -- lifecycle ---------------------------------------------------------

    @property
    def epoch(self) -> int:
        """Increments on every successful (re)connect."""
        return self._epoch

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closing

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._config.endpoint.startswith("wss://"):
            return None
        bundle = self._config.ca_bundle
        if bundle and Path(bundle).exists():
            return ssl.create_default_context(cafile=bundle)
        return ssl.create_default_context()

    async def connect(self) -> None:
        """Open the socket, retrying with exponential backoff and jitter."""
        delay = self._config.backoff_initial_s
        attempt = 0
        while not self._closing:
            attempt += 1
            try:
                self._ws = await connect(
                    self._config.url(),
                    ssl=self._ssl_context(),
                    open_timeout=self._config.open_timeout_s,
                    # Deriv is chatty enough that library-level keepalive can
                    # fight the application ping; we run our own.
                    ping_interval=None,
                    max_size=8 * 1024 * 1024,
                )
                self._epoch += 1
                if attempt > 1:
                    self.stats.reconnects += 1
                self._reader = asyncio.create_task(
                    self._read_loop(), name="deriv-reader")
                self._pinger = asyncio.create_task(
                    self._ping_loop(), name="deriv-pinger")
                log.info("connected to %s (epoch %d)",
                         self._config.endpoint, self._epoch)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry on anything
                jitter = random.uniform(0.5, 1.5)
                wait = min(delay * jitter, self._config.backoff_max_s)
                log.warning("connect attempt %d failed (%s); retrying in %.1fs",
                            attempt, exc, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2.0, self._config.backoff_max_s)

    async def close(self) -> None:
        self._closing = True
        for task in (self._pinger, self._reader):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        self._fail_outstanding(ConnectionLost("client closed"))

    async def __aenter__(self) -> "DerivClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- plumbing ----------------------------------------------------------

    def _fail_outstanding(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for sub in list(self._subs.values()):
            sub.closed = True
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)
        self._subs.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            async for raw in ws:
                self.stats.messages_received += 1
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("undecodable frame discarded (%d bytes)", len(raw))
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("read loop ended: %s", exc)
        finally:
            if not self._closing:
                self._fail_outstanding(ConnectionLost("socket closed"))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("req_id")
        if req_id is None:
            # Unsolicited server frames (rare). Recorded, not routed.
            log.debug("unsolicited message: %s", msg.get("msg_type"))
            return

        sub = self._subs.get(req_id)
        fut = self._pending.get(req_id)

        if "error" in msg:
            err = msg["error"] or {}
            exc = DerivError(err.get("code", "Unknown"),
                             err.get("message", ""), msg.get("echo_req"))
            self.stats.errors += 1
            if exc.is_rate_limit:
                self.stats.rate_limit_errors += 1
                log.warning("rate limited: %s", exc.message)
            if fut is not None and not fut.done():
                fut.set_exception(exc)
                self._pending.pop(req_id, None)
            elif sub is not None:
                # Mid-stream error terminates that stream only.
                log.warning("subscription %s error: %s", req_id, exc)
                sub.closed = True
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(None)
                self._subs.pop(req_id, None)
            return

        if sub is not None:
            if sub.subscription_id is None:
                sub.subscription_id = (msg.get("subscription") or {}).get("id")
            if sub.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                sub.dropped += 1
                self.stats.dropped_updates += 1
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(msg)

        if fut is not None and not fut.done():
            fut.set_result(msg)
            self._pending.pop(req_id, None)

    async def _ping_loop(self) -> None:
        """Application-level keepalive.

        A TCP connection can stay open long after the far end has stopped
        serving it. An unanswered application ping is the only reliable
        evidence the session is actually dead, so a timeout here forces the
        socket closed and lets the caller observe the reconnect.
        """
        interval = self._config.ping_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                await self.request(protocol.ping(), timeout=interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("keepalive failed (%s); dropping socket", exc)
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
                return

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionLost("not connected")
        await self._bucket.acquire()
        await self._ws.send(json.dumps(payload))
        self.stats.messages_sent += 1

    def _allocate(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req_id = self._next_req_id
        self._next_req_id += 1
        return req_id, {**payload, "req_id": req_id}

    # -- public API --------------------------------------------------------

    async def request(self, payload: dict[str, Any],
                      timeout: float | None = None) -> dict[str, Any]:
        """Send one request and await its matching response.

        Raises ``DerivError`` for an API-level error, ``ConnectionLost`` if the
        socket drops first, ``asyncio.TimeoutError`` if the reply never comes.
        """
        req_id, msg = self._allocate(payload)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send(msg)
            return await asyncio.wait_for(
                fut, timeout or self._config.request_timeout_s)
        finally:
            self._pending.pop(req_id, None)

    async def subscribe(self, payload: dict[str, Any],
                        timeout: float | None = None) -> Subscription:
        """Open a streaming subscription and await its first message.

        Awaiting the first message means a rejected subscription raises here,
        at the call site, rather than becoming a stream that silently never
        yields.
        """
        req_id, msg = self._allocate(payload)
        sub = Subscription(req_id=req_id, request=msg)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._subs[req_id] = sub
        try:
            await self._send(msg)
            await asyncio.wait_for(fut, timeout or self._config.request_timeout_s)
        except BaseException:
            self._subs.pop(req_id, None)
            raise
        finally:
            self._pending.pop(req_id, None)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        """Release a subscription server-side, then close its local queue.

        Failures are logged and swallowed: on rotation we are about to move on
        regardless, and a failed forget costs at most one stale stream that the
        next reconnect clears.
        """
        self._subs.pop(sub.req_id, None)
        sub.closed = True
        with contextlib.suppress(asyncio.QueueFull):
            sub.queue.put_nowait(None)
        if sub.subscription_id:
            try:
                await self.request(protocol.forget(sub.subscription_id))
            except Exception as exc:  # noqa: BLE001
                log.debug("forget %s failed: %s", sub.subscription_id, exc)

    async def forget_all(self, *types: str) -> None:
        try:
            await self.request(protocol.forget_all(*types))
        except Exception as exc:  # noqa: BLE001
            log.debug("forget_all failed: %s", exc)


async def drain(sub: Subscription) -> list[dict[str, Any]]:
    """Take everything currently queued without blocking."""
    out: list[dict[str, Any]] = []
    while True:
        try:
            item = sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            return out
        if item is None:
            sub.closed = True
            return out
        out.append(item)


def now_ms() -> int:
    return int(time.time() * 1000)
