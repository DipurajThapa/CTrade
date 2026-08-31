import json
from pathlib import Path

import pytest
import yaml

from deriv_census.cli import main, run_preflight
from deriv_census.config import (CensusConfig, ConnectionConfig, DecisionConfig,
                                 GridConfig, RateLimitConfig, StorageConfig)
from deriv_census.storage import PROPOSALS, TICKS, CensusStore

from .fake_deriv import FakeConfig, FakeDerivServer


def preflight_config(server, tmp_path, **grid) -> CensusConfig:
    return CensusConfig(
        connection=ConnectionConfig(endpoint=server.endpoint, app_id="1",
                                    request_timeout_s=10.0,
                                    ping_interval_s=3600.0),
        rate_limit=RateLimitConfig(requests_per_minute=6000),
        grid=GridConfig(durations_seconds=[300], **grid),
        storage=StorageConfig(root=str(tmp_path)))


async def test_preflight_passes_against_a_conforming_server(tmp_path, capsys):
    async with FakeDerivServer() as server:
        code = await run_preflight(preflight_config(server, tmp_path))
    out = capsys.readouterr().out
    assert code == 0
    assert "PREFLIGHT PASSED" in out
    assert "FAIL" not in out
    # It must actually surface the economics, not just say "ok".
    assert "house margin" in out and "break-even" in out


async def test_preflight_fails_when_a_contract_type_is_missing(tmp_path, capsys):
    async with FakeDerivServer(FakeConfig(
            contract_types=["CALL", "PUT"])) as server:
        code = await run_preflight(preflight_config(server, tmp_path))
    out = capsys.readouterr().out
    assert code == 1
    assert "PREFLIGHT FAILED" in out
    assert "CALLE/PUTE" in out


async def test_preflight_refuses_to_bless_a_run_it_could_not_verify(
        tmp_path, capsys):
    """A closed market cannot validate quoting, so preflight must not pass."""
    async with FakeDerivServer(FakeConfig(
            closed={"frxEURUSD", "frxGBPUSD"})) as server:
        code = await run_preflight(preflight_config(server, tmp_path))
    assert code == 1
    assert "re-run preflight during market hours" in capsys.readouterr().out


async def test_preflight_reports_an_unreachable_endpoint_cleanly(tmp_path, capsys):
    cfg = CensusConfig(
        connection=ConnectionConfig(endpoint="ws://127.0.0.1:1", app_id="1",
                                    open_timeout_s=0.3, backoff_initial_s=0.05,
                                    backoff_max_s=0.05),
        storage=StorageConfig(root=str(tmp_path)))
    assert await run_preflight(cfg) == 1
    assert "[FAIL] websocket connect" in capsys.readouterr().out


def test_analyse_writes_machine_readable_outputs(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    with CensusStore(data) as store:
        for i in range(60):
            store.write(PROPOSALS, {
                "symbol": "frxEURUSD", "contract_type": "CALL",
                "variant": "strict", "duration_s": 300, "b": 0.35,
                "sub_id": "s", "ts_ms": 1_700_000_000_000 + i * 1000})
        for epoch in range(3000):
            store.write(TICKS, {"symbol": "frxEURUSD", "tick_epoch": epoch,
                                "quote": 1.1 + (epoch % 7) * 1e-05,
                                "pip_size": 1e-05})

    cfg_path = tmp_path / "census.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "storage": {"root": str(data)},
        "decision": {"min_proposals_per_cell": 10,
                     "min_settlement_samples_per_cell": 10}}))
    monkeypatch.chdir(tmp_path)

    assert main(["-c", str(cfg_path), "analyse", "--html",
                 str(tmp_path / "r.html")]) == 0

    out = capsys.readouterr().out
    assert "VERDICT: STOP" in out
    verdict = json.loads((tmp_path / "reports" / "verdict.json").read_text())
    assert verdict["verdict"] == "STOP"
    assert (tmp_path / "reports" / "cells.csv").exists()
    assert "<table>" in (tmp_path / "r.html").read_text()


def test_analyse_on_an_empty_capture_does_not_crash(tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "census.yaml"
    cfg_path.write_text(yaml.safe_dump({"storage": {"root": str(tmp_path / "d")}}))
    monkeypatch.chdir(tmp_path)
    assert main(["-c", str(cfg_path), "analyse", "--html", ""]) == 0
    assert "INSUFFICIENT_DATA" in capsys.readouterr().out


def test_missing_config_falls_back_to_defaults_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["-c", "nope.yaml", "analyse", "--html", ""]) == 0


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit):
        main(["frobnicate"])
