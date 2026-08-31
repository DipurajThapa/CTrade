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


async def test_preflight_dump_captures_every_exchange_verbatim(tmp_path):
    """The dump is the evidence: it lets protocol.py be checked against real
    Deriv responses by someone who cannot reach the API themselves."""
    dump = tmp_path / "capture.json"
    async with FakeDerivServer() as server:
        await run_preflight(preflight_config(server, tmp_path), str(dump))

    payload = json.loads(dump.read_text())
    labels = [e["label"] for e in payload["exchanges"]]
    assert "ping" in labels
    # Every attempted request shape is recorded under its own label, which is
    # what makes an accepted-but-empty response diagnosable.
    assert any(l.startswith("active_symbols:") for l in labels)
    assert any(l.startswith("contracts_for:") for l in labels)
    assert any(l.startswith("proposal:CALL:") for l in labels)
    assert any(l.startswith("proposal:CALLE:") for l in labels)
    assert "tick" in labels

    # Responses must be stored verbatim, not summarised, or they cannot be
    # used to verify parsing.
    active = next(e for e in payload["exchanges"]
                  if e["label"].startswith("active_symbols:"))
    assert "active_symbols" in active["response"]
    assert active["response"]["active_symbols"][0]["symbol"]

    proposal = next(e for e in payload["exchanges"]
                    if e["label"].startswith("proposal:CALL:"))
    assert proposal["response"]["proposal"]["payout"] > 0
    assert proposal["request"]["basis"] == "stake"

    assert payload["checks"] and all(c["passed"] for c in payload["checks"])


async def test_preflight_dump_records_failures_too(tmp_path):
    """An error response is exactly the evidence needed to explain a failure."""
    dump = tmp_path / "capture.json"
    async with FakeDerivServer(FakeConfig(
            fail_every_nth_proposal=1)) as server:
        await run_preflight(preflight_config(server, tmp_path), str(dump))
    payload = json.loads(dump.read_text())
    errored = [e for e in payload["exchanges"] if "error" in e]
    assert errored
    assert errored[0]["error"]["code"] == "RateLimit"


async def test_preflight_dump_carries_no_credentials(tmp_path):
    """The capture is meant to be shared, so it must contain nothing secret.
    The client has no authentication path, so this is structural."""
    dump = tmp_path / "capture.json"
    async with FakeDerivServer() as server:
        await run_preflight(preflight_config(server, tmp_path), str(dump))
    text = dump.read_text().lower()
    for secret in ("authorize", "api_token", "\"token\"", "password",
                   "loginid", "balance"):
        assert secret not in text, f"capture leaked {secret}"


async def test_preflight_dump_survives_a_failed_run(tmp_path):
    """A partial capture is still evidence; it must be written on the way out."""
    dump = tmp_path / "capture.json"
    async with FakeDerivServer(FakeConfig(
            contract_types=["CALL", "PUT"])) as server:
        code = await run_preflight(preflight_config(server, tmp_path), str(dump))
    assert code == 1
    assert dump.exists()
    assert json.loads(dump.read_text())["exchanges"]


async def test_preflight_without_dump_writes_nothing(tmp_path):
    async with FakeDerivServer() as server:
        await run_preflight(preflight_config(server, tmp_path))
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("b,expect", [
    (0.99, "Worth measuring properly"),
    (0.97, "Worth measuring properly"),
    (0.93, "Borderline"),
    (0.90, "Borderline"),
    (0.80, "too big"),
    (0.45, "too big"),
])
def test_plain_english_verdict_tracks_the_payout(b, expect):
    """A non-technical reader must get the right answer from prose alone."""
    from deriv_census.cli import plain_english_summary
    assert expect in plain_english_summary({"CALL": b}, 300)


def test_plain_english_states_the_break_even_win_rate_correctly():
    from deriv_census.cli import plain_english_summary
    text = plain_english_summary({"CALL": 0.80}, 300)
    assert "55.6% of the time" in text     # 1/(1+0.80)
    assert "$18.00 if you win" in text     # $10 stake -> $18 gross
    assert "5 minutes" in text


def test_plain_english_always_warns_that_one_quote_is_not_the_verdict():
    """Preflight sees one snapshot and no tie rate. Saying so is not optional."""
    from deriv_census.cli import plain_english_summary
    for b in (0.99, 0.90, 0.40):
        assert "this is ONE quote" in plain_english_summary({"CALL": b}, 300)


def test_plain_english_uses_the_median_across_contract_types():
    from deriv_census.cli import plain_english_summary
    text = plain_english_summary({"CALL": 0.90, "PUT": 0.90, "CALLE": 0.80}, 300)
    assert "52.6% of the time" in text     # median is 0.90, not the mean


async def test_preflight_prints_the_plain_english_box(tmp_path, capsys):
    async with FakeDerivServer(FakeConfig(payout=0.45,
                                          payout_jitter=0.0)) as server:
        await run_preflight(preflight_config(server, tmp_path))
    out = capsys.readouterr().out
    assert "WHAT THIS MEANS" in out
    assert "too big" in out
