"""The dashboard must never interfere with the capture it observes."""

import json
import threading
import time
import urllib.request

import pytest
import yaml

from deriv_census.config import CensusConfig, DecisionConfig, StorageConfig
from deriv_census.server import (ReportCache, capture_health, render_page,
                                 serve, tail_jsonl)
from deriv_census.storage import EVENTS, PROPOSALS, TICKS, CensusStore


def seed(root, *, b=0.45, n=60, heartbeat=True, finished=False):
    with CensusStore(root) as store:
        for i in range(n):
            store.write(PROPOSALS, {
                "symbol": "frxEURUSD", "contract_type": "CALL",
                "variant": "strict", "duration_s": 300, "b": b, "sub_id": "s",
                "ts_ms": 1_700_000_000_000 + i * 1000})
        for epoch in range(3000):
            store.write(TICKS, {"symbol": "frxEURUSD", "tick_epoch": epoch,
                                "quote": 1.1 + (epoch % 7) * 1e-05,
                                "pip_size": 1e-05})
        store.event("run_started", endpoint="ws://x")
        if heartbeat:
            store.event("heartbeat", proposals_recorded=n, ticks_recorded=3000,
                        rotations=4, elapsed_hours=1.5,
                        client={"reconnects": 2})
        if finished:
            store.event("run_finished", proposals_recorded=n)


def config_for(root):
    return CensusConfig(
        storage=StorageConfig(root=str(root)),
        decision=DecisionConfig(min_proposals_per_cell=10,
                                min_settlement_samples_per_cell=10))


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode()


@pytest.fixture
def running_server(tmp_path):
    seed(tmp_path)
    httpd = serve(config_for(tmp_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dashboard_serves_the_verdict(running_server):
    status, body = get(running_server + "/")
    assert status == 200
    assert "Deriv payout census" in body
    assert "STOP" in body                  # b=0.45 is unplayable
    assert "Per-cell economics" in body


def test_json_endpoints(running_server):
    status, body = get(running_server + "/api/verdict")
    assert status == 200
    payload = json.loads(body)
    assert payload["verdict"] == "STOP"
    assert payload["coverage"]["proposal_records"] == 60

    status, body = get(running_server + "/api/cells")
    cells = json.loads(body)
    assert cells and cells[0]["symbol"] == "frxEURUSD"
    assert cells[0]["required_edge"] > 0.1

    status, body = get(running_server + "/api/health")
    health = json.loads(body)
    assert health["state"] == "running"
    assert health["proposals_recorded"] == 60
    assert health["bytes_on_disk"] > 0


def test_unknown_path_is_404_not_a_crash(running_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(running_server + "/wat")
    assert excinfo.value.code == 404


def test_dashboard_and_cli_cannot_disagree(tmp_path):
    """The page reuses the CLI's own renderer, so the numbers are identical."""
    from deriv_census.analysis import build_report
    seed(tmp_path)
    report = build_report(tmp_path, config_for(tmp_path).decision)
    page = render_page(ReportCache(config_for(tmp_path)).get(),
                       capture_health(tmp_path), 30)
    assert report.overall_verdict in page
    assert f"{report.cells[0].b_median:.4f}" in page


def test_report_is_cached_so_the_dashboard_does_not_fight_the_capture(tmp_path):
    """Rebuilding reads the whole capture, which reaches gigabytes."""
    seed(tmp_path)
    cache = ReportCache(config_for(tmp_path), ttl_s=60.0)
    first = cache.get()
    second = cache.get()
    assert first.report is second.report        # not rebuilt
    assert first.built_at == second.built_at


def test_expired_cache_rebuilds(tmp_path):
    seed(tmp_path)
    cache = ReportCache(config_for(tmp_path), ttl_s=0.0)
    assert cache.get().built_at < cache.get().built_at


def test_an_empty_capture_renders_a_waiting_page(tmp_path):
    cache = ReportCache(config_for(tmp_path))
    page = render_page(cache.get(), capture_health(tmp_path), 30)
    assert "INSUFFICIENT_DATA" in page or "WAITING" in page


def test_a_stalled_capture_is_visible_not_silent(tmp_path):
    """A capture that died at 3am must be obvious on the page."""
    with CensusStore(tmp_path) as store:
        store.write(EVENTS, {"kind": "heartbeat", "proposals_recorded": 10,
                             "ts_ms": int((time.time() - 3600) * 1000)})
    health = capture_health(tmp_path)
    assert health["state"] == "stalled"
    assert health["seconds_since_heartbeat"] > 3000


def test_finished_and_starting_states(tmp_path):
    assert capture_health(tmp_path)["state"] == "starting"
    seed(tmp_path, finished=True)
    assert capture_health(tmp_path)["state"] == "finished"


def test_tail_reads_only_the_end_of_a_large_file(tmp_path):
    path = tmp_path / "big.jsonl"
    with path.open("w") as handle:
        for i in range(50_000):
            handle.write(json.dumps({"i": i, "pad": "x" * 100}) + "\n")
    records = tail_jsonl(path, limit=5)
    assert [r["i"] for r in records] == [49995, 49996, 49997, 49998, 49999]


def test_tail_tolerates_a_truncated_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"i": 1}\n{"i": 2}\n{"i": 3, "trunc')
    assert [r["i"] for r in tail_jsonl(path)] == [1, 2]


def test_tail_of_a_missing_file_is_empty(tmp_path):
    assert tail_jsonl(tmp_path / "nope.jsonl") == []


def test_a_failed_report_build_keeps_serving_the_previous_one(tmp_path, monkeypatch):
    """Mid-capture, stale data with a visible warning beats a blank page."""
    seed(tmp_path)
    cache = ReportCache(config_for(tmp_path), ttl_s=0.0)
    good = cache.get().report
    assert good is not None

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk went away")

    monkeypatch.setattr("deriv_census.server.build_report", boom)
    state = cache.get()
    assert state.report is good              # previous result retained
    assert state.error == "disk went away"

    page = render_page(state, capture_health(tmp_path), 30)
    assert "Last report build failed" in page
    assert "disk went away" in page


def test_binds_to_localhost_by_default(tmp_path):
    httpd = serve(config_for(tmp_path), port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()
