import json

import pytest

from deriv_census.storage import (EVENTS, PROPOSALS, TICKS, CensusStore,
                                  export_parquet, read_stream, utc_date)


def test_round_trip(tmp_path):
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"symbol": "frxEURUSD", "b": 0.92})
        store.write(TICKS, {"symbol": "frxEURUSD", "quote": 1.1})
        store.event("run_started", endpoint="ws://x")
    assert [r["b"] for r in read_stream(tmp_path, PROPOSALS)] == [0.92]
    assert [r["kind"] for r in read_stream(tmp_path, EVENTS)] == ["run_started"]


def test_timestamp_is_added_and_partitions_by_utc_date(tmp_path):
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"symbol": "X"})
    record = next(iter(read_stream(tmp_path, PROPOSALS)))
    assert isinstance(record["ts_ms"], int)
    assert (tmp_path / PROPOSALS / f"{utc_date(record['ts_ms'])}.jsonl").exists()


def test_explicit_timestamp_selects_the_partition(tmp_path):
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"symbol": "X", "ts_ms": 1_700_000_000_000})
    assert (tmp_path / PROPOSALS / "2023-11-14.jsonl").exists()


def test_a_truncated_final_line_does_not_lose_the_file(tmp_path):
    """The expected outcome of an interrupted 14-day run."""
    with CensusStore(tmp_path) as store:
        for i in range(5):
            store.write(PROPOSALS, {"i": i})
    path = next((tmp_path / PROPOSALS).glob("*.jsonl"))
    with path.open("a") as handle:
        handle.write('{"i": 5, "trunc')          # power cut mid-write
    recovered = [r["i"] for r in read_stream(tmp_path, PROPOSALS)]
    assert recovered == [0, 1, 2, 3, 4]


def test_strict_mode_surfaces_corruption_instead_of_hiding_it(tmp_path):
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"i": 0})
    path = next((tmp_path / PROPOSALS).glob("*.jsonl"))
    with path.open("a") as handle:
        handle.write("{bad\n")
    with pytest.raises(json.JSONDecodeError):
        list(read_stream(tmp_path, PROPOSALS, strict=True))


def test_appends_across_store_instances(tmp_path):
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"i": 0})
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"i": 1})
    assert [r["i"] for r in read_stream(tmp_path, PROPOSALS)] == [0, 1]


def test_unknown_stream_rejected(tmp_path):
    with CensusStore(tmp_path) as store:
        with pytest.raises(ValueError):
            store.write("not_a_stream", {})


def test_missing_stream_reads_as_empty(tmp_path):
    assert list(read_stream(tmp_path / "nope", PROPOSALS)) == []


def test_non_serialisable_values_do_not_abort_the_run(tmp_path):
    """Losing one field's fidelity beats crashing an unattended capture."""
    class Weird:
        def __repr__(self): return "<weird>"
    with CensusStore(tmp_path) as store:
        store.write(PROPOSALS, {"x": Weird()})
    assert next(iter(read_stream(tmp_path, PROPOSALS)))["x"] == "<weird>"


def test_parquet_export(tmp_path):
    with CensusStore(tmp_path) as store:
        for i in range(3):
            store.write(PROPOSALS, {"symbol": "X", "b": 0.9 + i / 100})
    rows = export_parquet(tmp_path, PROPOSALS, tmp_path / "out.parquet")
    assert rows == 3 and (tmp_path / "out.parquet").exists()
    assert export_parquet(tmp_path, TICKS, tmp_path / "empty.parquet") == 0
