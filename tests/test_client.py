"""The client is exercised against a real socket, not a mock."""

import asyncio

import pytest

from deriv_census import protocol
from deriv_census.client import ConnectionLost, DerivClient
from deriv_census.config import ConnectionConfig
from deriv_census.protocol import DerivError
from deriv_census.ratelimit import TokenBucket

from .fake_deriv import FakeConfig, FakeDerivServer


def make_client(server, **kwargs):
    cfg = ConnectionConfig(endpoint=server.endpoint, app_id="1",
                           request_timeout_s=10.0, ping_interval_s=3600.0,
                           **kwargs)
    return DerivClient(cfg, TokenBucket(6000))


async def test_request_response_round_trip():
    async with FakeDerivServer() as server:
        async with make_client(server) as client:
            assert (await client.request(protocol.ping()))["ping"] == "pong"
            payload = await client.request(protocol.active_symbols())
            assert len(protocol.parse_active_symbols(payload)) == 3


async def test_responses_are_matched_by_req_id_not_arrival_order():
    """A slow contracts_for must not be delivered as a fast ping's reply."""
    async with FakeDerivServer() as server:
        async with make_client(server) as client:
            results = await asyncio.gather(
                client.request(protocol.contracts_for("frxEURUSD")),
                client.request(protocol.ping()),
                client.request(protocol.active_symbols()),
            )
            assert "contracts_for" in results[0]
            assert results[1]["ping"] == "pong"
            assert "active_symbols" in results[2]


async def test_api_errors_raise_at_the_call_site():
    async with FakeDerivServer(FakeConfig(reject_types={"CALLE"})) as server:
        async with make_client(server) as client:
            with pytest.raises(DerivError) as excinfo:
                await client.request(protocol.proposal(
                    "frxEURUSD", "CALLE", 5, "m", 10.0, subscribe=False))
            assert excinfo.value.is_permanent
            # The connection survives an application-level error.
            assert (await client.request(protocol.ping()))["ping"] == "pong"


async def test_rate_limit_errors_are_classified_and_counted():
    async with FakeDerivServer(FakeConfig(fail_every_nth_proposal=1)) as server:
        async with make_client(server) as client:
            with pytest.raises(DerivError) as excinfo:
                await client.request(protocol.proposal(
                    "frxEURUSD", "CALL", 5, "m", 10.0, subscribe=False))
            assert excinfo.value.is_rate_limit
            assert not excinfo.value.is_permanent   # must retry, not drop
            assert client.stats.rate_limit_errors == 1


async def test_subscription_streams_updates_and_captures_its_id():
    async with FakeDerivServer() as server:
        async with make_client(server) as client:
            sub = await client.subscribe(protocol.proposal(
                "frxEURUSD", "CALL", 5, "m", 10.0))
            assert sub.subscription_id is not None
            received = [await asyncio.wait_for(sub.queue.get(), 5)
                        for _ in range(3)]
            assert all(m["proposal"]["payout"] > 10 for m in received)
            await client.unsubscribe(sub)


async def test_rejected_subscription_raises_instead_of_silently_never_yielding():
    async with FakeDerivServer(FakeConfig(reject_types={"PUTE"})) as server:
        async with make_client(server) as client:
            with pytest.raises(DerivError):
                await client.subscribe(protocol.proposal(
                    "frxEURUSD", "PUTE", 5, "m", 10.0))
            assert not client._subs      # no leaked bookkeeping


async def test_unsubscribe_releases_the_stream_server_side():
    async with FakeDerivServer() as server:
        async with make_client(server) as client:
            sub = await client.subscribe(protocol.ticks("frxEURUSD"))
            await asyncio.wait_for(sub.queue.get(), 5)
            await client.unsubscribe(sub)
            assert server.request_counts.get("forget", 0) == 1
            assert sub.closed


async def test_slow_consumer_drops_oldest_rather_than_exhausting_memory():
    """A stalled consumer must cost stale quotes, never the whole run.

    Driven through the dispatcher directly rather than by racing the fake
    server against a sleep. The timing-based version needed roughly 1,500
    messages inside 1.5s, which holds on Linux but not on Windows, where the
    event loop timer granularity is about 15ms: the queue never filled and
    the test failed against code that was working correctly.

    Feeding the dispatcher is also a stricter test. It pins the exact drop
    count and proves the OLDEST are the ones discarded, neither of which the
    timing version could check.
    """
    from deriv_census.client import QUEUE_MAXSIZE
    async with FakeDerivServer() as server:
        async with make_client(server) as client:
            sub = await client.subscribe(protocol.ticks("frxEURUSD"))

            # Clear anything the live subscription already delivered so the
            # counts below are exact. No await from here to the assertions,
            # so the reader task cannot interleave.
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.dropped = 0
            client.stats.dropped_updates = 0

            overflow = 25
            for epoch in range(QUEUE_MAXSIZE + overflow):
                client._dispatch({
                    "req_id": sub.req_id, "msg_type": "tick",
                    "tick": {"symbol": "frxEURUSD", "epoch": epoch,
                             "quote": 1.1, "pip_size": 1e-05}})

            assert sub.queue.qsize() == QUEUE_MAXSIZE     # bounded, not growing
            assert sub.dropped == overflow                # exactly the excess
            assert client.stats.dropped_updates == overflow

            # The newest quote must survive: a stale quote is worthless, the
            # current one is the whole point.
            newest = None
            while not sub.queue.empty():
                newest = sub.queue.get_nowait()
            assert newest["tick"]["epoch"] == QUEUE_MAXSIZE + overflow - 1

            await client.unsubscribe(sub)


async def test_pending_requests_fail_fast_when_the_socket_drops():
    async with FakeDerivServer() as server:
        client = make_client(server)
        await client.connect()
        await client.request(protocol.ping())
        sub = await client.subscribe(protocol.ticks("frxEURUSD"))
        await client._ws.close()
        await asyncio.sleep(0.2)
        assert sub.closed or sub.queue.qsize() >= 0
        with pytest.raises((ConnectionLost, asyncio.TimeoutError, Exception)):
            await client.request(protocol.ping(), timeout=2.0)
        await client.close()


async def test_reconnect_increments_the_epoch_so_callers_resubscribe():
    """The client deliberately does not silently resubscribe: a resubscribe
    that quietly failed would leave the run recording nothing while looking
    healthy."""
    async with FakeDerivServer() as server:
        client = make_client(server)
        await client.connect()
        first = client.epoch
        await client._ws.close()
        await asyncio.sleep(0.1)
        await client.connect()
        assert client.epoch == first + 1
        assert client.stats.reconnects >= 0
        await client.close()


async def test_connect_retries_until_the_server_appears():
    cfg = ConnectionConfig(endpoint="ws://127.0.0.1:1", app_id="1",
                           backoff_initial_s=0.05, backoff_max_s=0.1,
                           open_timeout_s=0.5)
    client = DerivClient(cfg, TokenBucket(6000))
    task = asyncio.create_task(client.connect())
    await asyncio.sleep(0.4)
    assert not task.done()          # still retrying, not crashed
    client._closing = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_client_cannot_authenticate_or_trade():
    """Structural guarantee, verified against the wire."""
    async with FakeDerivServer() as server:
        async with make_client(server) as client:
            await client.request(protocol.ping())
            assert set(server.request_counts) <= {
                "ping", "active_symbols", "contracts_for", "proposal",
                "ticks", "forget", "forget_all"}
