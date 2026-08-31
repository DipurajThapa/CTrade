"""Full pipeline: runner -> socket -> capture -> analysis -> verdict.

Nothing is mocked. If the census works in these tests it works because the
components genuinely fit together, not because a stub agreed with them.
"""

import asyncio
import time

import pytest

from deriv_census.analysis import GO, INSUFFICIENT, STOP, build_report
from deriv_census.config import (CensusConfig, ConnectionConfig, DecisionConfig,
                                 GridConfig, RateLimitConfig, SamplingConfig,
                                 StorageConfig)
from deriv_census.report import render_html, render_text
from deriv_census.runner import CensusRunner
from deriv_census.storage import (EVENTS, PROPOSALS, TICKS, CensusStore,
                                  read_stream)

from .fake_deriv import FakeConfig, FakeDerivServer


def make_config(server, tmp_path, **overrides) -> CensusConfig:
    cfg = CensusConfig(
        connection=ConnectionConfig(endpoint=server.endpoint, app_id="1",
                                    request_timeout_s=10.0,
                                    ping_interval_s=3600.0),
        rate_limit=RateLimitConfig(requests_per_minute=6000,
                                   max_concurrent_proposals=4,
                                   max_concurrent_ticks=2),
        grid=GridConfig(durations_seconds=[120, 300],
                        variants=["strict", "equals"], directions=["rise"]),
        sampling=SamplingConfig(dwell_seconds=1.0, rotation_pause_seconds=0.05,
                                rediscover_every_minutes=60.0),
        decision=DecisionConfig(min_proposals_per_cell=10,
                                min_settlement_samples_per_cell=10),
        storage=StorageConfig(root=str(tmp_path), flush_interval_s=0.1))
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.validate()
    return cfg


async def capture(server, tmp_path, seconds=6.0, **overrides):
    cfg = make_config(server, tmp_path, **overrides)
    with CensusStore(tmp_path, 0.1) as store:
        runner = CensusRunner(cfg, store)
        await runner.run(deadline_epoch=time.time() + seconds)
    return cfg, runner


async def test_full_capture_then_verdict(tmp_path):
    async with FakeDerivServer(FakeConfig(payout=0.92)) as server:
        cfg, runner = await capture(server, tmp_path)

    assert runner.stats.proposals_recorded > 0
    assert runner.stats.ticks_recorded > 0
    assert runner.stats.rotations > 0

    proposals = list(read_stream(tmp_path, PROPOSALS))
    ticks = list(read_stream(tmp_path, TICKS))
    assert len(proposals) > 10 and len(ticks) > 10

    row = proposals[0]
    assert row["b"] == pytest.approx((row["payout"] - row["stake"]) / row["stake"])
    assert row["contract_type"] in {"CALL", "CALLE"}
    assert row["duration_s"] in {120, 300}
    assert row["sub_id"]

    report = build_report(tmp_path, cfg.decision)
    assert report.overall_verdict in {GO, "CONDITIONAL", STOP, INSUFFICIENT}
    assert report.coverage["proposal_records"] == len(proposals)
    assert report.cells

    # Economics must be internally consistent for every reported cell.
    for cell in report.cells:
        assert cell.breakeven_probability == pytest.approx(
            1 / (1 + cell.b_median))
        assert cell.house_margin == pytest.approx(
            cell.breakeven_probability - 0.5)
        # Ties penalise strict contracts and subsidise equals contracts, so
        # the required edge sits on opposite sides of the raw margin.
        if cell.variant == "strict":
            assert cell.required_edge >= cell.house_margin - 1e-12
        else:
            assert cell.required_edge <= cell.house_margin + 1e-12
        if cell.tie_rate == 0.0:
            assert cell.required_edge == pytest.approx(cell.house_margin)

    assert "VERDICT" in render_text(report)
    assert "<table>" in render_html(report)


async def test_a_generous_payout_produces_go_and_a_punitive_one_stop(tmp_path):
    """The decision rule must actually respond to the measurement."""
    async with FakeDerivServer(FakeConfig(payout=0.999, payout_jitter=0.0005,
                                          tick_sigma=6e-5)) as server:
        cfg, _ = await capture(server, tmp_path / "rich", seconds=6.0,
                               storage=StorageConfig(root=str(tmp_path / "rich"),
                                                     flush_interval_s=0.1))
    rich = build_report(tmp_path / "rich", cfg.decision)

    async with FakeDerivServer(FakeConfig(payout=0.35, payout_jitter=0.005,
                                          tick_sigma=6e-5)) as server:
        cfg2, _ = await capture(server, tmp_path / "poor", seconds=6.0,
                                storage=StorageConfig(root=str(tmp_path / "poor"),
                                                      flush_interval_s=0.1))
    poor = build_report(tmp_path / "poor", cfg2.decision)

    assert rich.best() is not None and poor.best() is not None
    assert rich.best().required_edge < poor.best().required_edge
    assert poor.overall_verdict == STOP


async def test_synthetic_instruments_never_enter_the_capture(tmp_path):
    """The fake advertises a volatility index; the census must ignore it."""
    async with FakeDerivServer() as server:
        await capture(server, tmp_path, seconds=4.0)
    assert {r["symbol"] for r in read_stream(tmp_path, PROPOSALS)} == {
        "frxEURUSD", "frxGBPUSD"}


async def test_closed_markets_are_skipped_without_stalling_the_run(tmp_path):
    async with FakeDerivServer(FakeConfig(closed={"frxGBPUSD"})) as server:
        _, runner = await capture(server, tmp_path, seconds=6.0)
    symbols = {r["symbol"] for r in read_stream(tmp_path, PROPOSALS)}
    assert symbols == {"frxEURUSD"}
    assert runner.stats.proposals_recorded > 0
    # Closed is transient, so the cell must be cooled down, never dropped.
    assert runner.stats.cells_permanently_dropped == 0
    assert any(e["kind"] == "cell_cooldown" for e in read_stream(tmp_path, EVENTS))


async def test_permanently_rejected_cells_are_dropped_once_not_retried_forever(
        tmp_path):
    async with FakeDerivServer(FakeConfig(reject_types={"CALLE"})) as server:
        _, runner = await capture(server, tmp_path, seconds=6.0)
    assert runner.stats.cells_permanently_dropped > 0
    assert {r["contract_type"] for r in read_stream(tmp_path, PROPOSALS)} == {"CALL"}
    dropped = [e for e in read_stream(tmp_path, EVENTS) if e["kind"] == "cell_dropped"]
    keys = [e["cell"] for e in dropped]
    assert len(keys) == len(set(keys))          # dropped once, not repeatedly


async def test_transient_errors_do_not_kill_the_run(tmp_path):
    async with FakeDerivServer(FakeConfig(fail_every_nth_proposal=3)) as server:
        _, runner = await capture(server, tmp_path, seconds=6.0)
    assert runner.stats.proposals_recorded > 0   # kept going despite errors
    assert runner.client.stats.rate_limit_errors > 0


async def test_run_is_auditable_from_the_event_stream(tmp_path):
    """A census whose coverage cannot be reconstructed is not evidence."""
    async with FakeDerivServer() as server:
        await capture(server, tmp_path, seconds=5.0)
    kinds = [e["kind"] for e in read_stream(tmp_path, EVENTS)]
    assert kinds[0] == "run_started"
    assert "discovery" in kinds
    assert kinds[-1] == "run_finished"
    started = next(e for e in read_stream(tmp_path, EVENTS)
                   if e["kind"] == "run_started")
    assert started["config"]["grid"]["stake"] == 10.0   # config is recorded


async def test_capture_survives_cancellation_without_losing_data(tmp_path):
    """A 14-day run will be interrupted; captured data must still be readable."""
    async with FakeDerivServer() as server:
        cfg = make_config(server, tmp_path)
        with CensusStore(tmp_path, 0.1) as store:
            runner = CensusRunner(cfg, store)
            task = asyncio.create_task(
                runner.run(deadline_epoch=time.time() + 60))
            await asyncio.sleep(3.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert len(list(read_stream(tmp_path, PROPOSALS))) > 0
    report = build_report(tmp_path, cfg.decision)
    assert report.coverage["proposal_records"] > 0


async def test_subscriptions_are_released_on_rotation(tmp_path):
    """Otherwise a 14-day run leaks streams until the venue cuts it off."""
    async with FakeDerivServer() as server:
        await capture(server, tmp_path, seconds=6.0)
        opened = server.request_counts.get("proposal", 0)
        released = (server.request_counts.get("forget", 0)
                    + server.request_counts.get("forget_all", 0))
    assert opened > 4
    assert released >= opened * 0.5


async def test_capture_works_when_only_a_later_symbol_shape_is_accepted(tmp_path):
    """The live failure: the first request shape returns an empty list. The
    run must find a shape that works rather than reporting no instruments."""
    async with FakeDerivServer(FakeConfig(
            active_symbols_requires={"landing_company_short": "svg"})) as server:
        _, runner = await capture(server, tmp_path, seconds=6.0)

    assert runner.stats.proposals_recorded > 0
    probes = [e for e in read_stream(tmp_path, EVENTS)
              if e["kind"] == "active_symbols_probe"]
    assert probes, "every attempt must be recorded for diagnosis"
    assert not probes[0]["count"]                      # first shape found nothing
    assert probes[-1]["count"] > 0                     # a later one worked
    assert probes[-1]["variant"] == "brief+svg"
