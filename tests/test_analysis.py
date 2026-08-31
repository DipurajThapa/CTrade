import math

import numpy as np
import pandas as pd
import pytest

from deriv_census.analysis import (CONDITIONAL, GO, INSUFFICIENT, STOP,
                                   analyse_cells, build_report, classify,
                                   load_proposals, load_ticks,
                                   measure_settlement, payout_drift)
from deriv_census.config import DecisionConfig
from deriv_census.settlement import SettlementOutcomes
from deriv_census.storage import PROPOSALS, TICKS, CensusStore

LOOSE = DecisionConfig(min_proposals_per_cell=5,
                       min_settlement_samples_per_cell=5)


def seed(root, *, b=0.92, n=50, symbol="frxEURUSD", ct="CALL", dur=300,
         sub="s1"):
    with CensusStore(root) as store:
        for i in range(n):
            store.write(PROPOSALS, {
                "symbol": symbol, "contract_type": ct, "variant": "strict",
                "duration_s": dur, "b": b, "sub_id": sub,
                "ts_ms": 1_700_000_000_000 + i * 1000})


def outcomes(symbol="frxEURUSD", dur=300, tie=10, up=45, down=45):
    return SettlementOutcomes(symbol, dur, up, down, tie,
                              up // 3, down // 3, tie // 3, 0)


def test_classify_uses_the_preregistered_thresholds():
    d = DecisionConfig()
    assert classify(0.010, d) == GO
    assert classify(0.015, d) == GO            # inclusive at the boundary
    assert classify(0.020, d) == CONDITIONAL
    assert classify(0.030, d) == CONDITIONAL
    assert classify(0.031, d) == STOP
    assert classify(math.inf, d) == STOP


def test_generous_payout_with_no_ties_reaches_go(tmp_path):
    seed(tmp_path, b=0.99, n=50)
    cells = analyse_cells(load_proposals(tmp_path),
                          {("frxEURUSD", 300): outcomes(tie=0, up=50, down=50)},
                          LOOSE)
    assert cells[0].verdict == GO
    assert cells[0].required_edge < 0.015


def test_punitive_payout_reaches_stop(tmp_path):
    seed(tmp_path, b=0.30, n=50)
    cells = analyse_cells(load_proposals(tmp_path),
                          {("frxEURUSD", 300): outcomes(tie=0, up=50, down=50)},
                          LOOSE)
    assert cells[0].verdict == STOP
    assert cells[0].required_edge > 0.20


def test_ties_alone_can_flip_a_go_into_a_stop(tmp_path):
    """The finding that justifies measuring ticks at all."""
    seed(tmp_path, b=0.985, n=50)
    proposals = load_proposals(tmp_path)
    without = analyse_cells(
        proposals, {("frxEURUSD", 300): outcomes(tie=0, up=500, down=500)}, LOOSE)
    with_ties = analyse_cells(
        proposals, {("frxEURUSD", 300): outcomes(tie=60, up=470, down=470)}, LOOSE)
    assert without[0].verdict == GO
    assert with_ties[0].verdict in (CONDITIONAL, STOP)
    assert with_ties[0].required_edge > without[0].required_edge


def test_missing_tick_coverage_is_flagged_as_optimistic(tmp_path):
    """Absent ticks means no tie penalty, which understates the requirement.
    That must be visible, not silent."""
    seed(tmp_path, b=0.95, n=50)
    cells = analyse_cells(load_proposals(tmp_path), {}, LOOSE)
    assert cells[0].tie_rate == 0.0
    assert any("OPTIMISTIC" in note for note in cells[0].notes)


def test_thin_cells_are_withheld_rather_than_reported(tmp_path):
    seed(tmp_path, b=0.95, n=3)
    cells = analyse_cells(load_proposals(tmp_path),
                          {("frxEURUSD", 300): outcomes()},
                          DecisionConfig(min_proposals_per_cell=100))
    assert cells[0].verdict == INSUFFICIENT
    assert "only 3 proposals" in cells[0].notes[0]


def test_cells_are_ranked_best_first(tmp_path):
    seed(tmp_path, b=0.95, n=20, ct="CALL", sub="a")
    seed(tmp_path, b=0.60, n=20, ct="CALLE", sub="b")
    cells = analyse_cells(load_proposals(tmp_path),
                          {("frxEURUSD", 300): outcomes()}, LOOSE)
    assert cells[0].contract_type == "CALL"
    assert cells[0].required_edge < cells[1].required_edge


def test_adverse_case_uses_the_tenth_percentile_payout(tmp_path):
    with CensusStore(tmp_path) as store:
        for i in range(100):
            store.write(PROPOSALS, {
                "symbol": "frxEURUSD", "contract_type": "CALL",
                "variant": "strict", "duration_s": 300,
                "b": 0.95 if i >= 20 else 0.60, "sub_id": "s",
                "ts_ms": 1_700_000_000_000 + i * 1000})
    cells = analyse_cells(load_proposals(tmp_path),
                          {("frxEURUSD", 300): outcomes()}, LOOSE)
    assert cells[0].b_median == pytest.approx(0.95)
    assert cells[0].b_p10 < 0.95
    assert cells[0].required_edge_hi > cells[0].required_edge


def test_corrupt_payout_values_are_dropped(tmp_path):
    with CensusStore(tmp_path) as store:
        for b in (0.9, None, "nonsense", -5.0, 99.0, 0.9):
            store.write(PROPOSALS, {"symbol": "X", "contract_type": "CALL",
                                    "duration_s": 300, "b": b, "sub_id": "s"})
    assert len(load_proposals(tmp_path)) == 2


def test_duplicate_ticks_across_reconnects_are_deduplicated(tmp_path):
    with CensusStore(tmp_path) as store:
        for epoch in (1, 2, 2, 3):
            store.write(TICKS, {"symbol": "X", "tick_epoch": epoch,
                                "quote": 1.1, "pip_size": 1e-05})
    assert len(load_ticks(tmp_path)) == 3


def test_payout_drift_is_measured_per_subscription(tmp_path):
    with CensusStore(tmp_path) as store:
        for i, b in enumerate([0.90, 0.91, 0.90, 0.92]):
            store.write(PROPOSALS, {"symbol": "X", "contract_type": "CALL",
                                    "duration_s": 300, "b": b, "sub_id": "s1",
                                    "ts_ms": 1_700_000_000_000 + i * 1000})
    drift = payout_drift(load_proposals(tmp_path))
    assert drift["n"] == 3
    assert drift["max"] == pytest.approx(0.02, abs=1e-9)
    assert drift["share_nonzero"] == 1.0


def test_measure_settlement_spans_symbols_and_durations(tmp_path):
    with CensusStore(tmp_path) as store:
        for epoch in range(2000):
            store.write(TICKS, {"symbol": "frxEURUSD", "tick_epoch": epoch,
                                "quote": 1.10000 + (epoch % 3) * 1e-05,
                                "pip_size": 1e-05})
    result = measure_settlement(load_ticks(tmp_path), [120, 300])
    assert set(result) == {("frxEURUSD", 120), ("frxEURUSD", 300)}
    assert result[("frxEURUSD", 120)].total > 1000


def test_empty_capture_reports_insufficient_not_a_verdict(tmp_path):
    report = build_report(tmp_path, DecisionConfig())
    assert report.overall_verdict == INSUFFICIENT
    assert report.best() is None
    assert "continue capture" in report.rationale.lower()


def test_report_rationale_quotes_the_numbers_behind_the_verdict(tmp_path):
    seed(tmp_path, b=0.30, n=50)
    with CensusStore(tmp_path) as store:
        for epoch in range(3000):
            store.write(TICKS, {"symbol": "frxEURUSD", "tick_epoch": epoch,
                                "quote": 1.1 + (epoch % 7) * 1e-05,
                                "pip_size": 1e-05})
    report = build_report(tmp_path, LOOSE)
    assert report.overall_verdict == STOP
    assert "required directional edge" in report.rationale
    assert "do not spend the build" in report.rationale.lower()
    assert not report.to_frame().empty


def test_adverse_case_uses_the_tie_bound_that_hurts_each_variant(tmp_path):
    """Ties penalise strict contracts and subsidise equals ones, so the
    adverse tie bound differs by variant. Using the upper bound for both
    reports a fantastically favourable number as a stress case."""
    with CensusStore(tmp_path) as store:
        for ct in ("CALL", "CALLE"):
            for i in range(60):
                store.write(PROPOSALS, {
                    "symbol": "frxEURUSD", "contract_type": ct,
                    "variant": "strict" if ct == "CALL" else "equals",
                    "duration_s": 300, "b": 0.95, "sub_id": ct,
                    "ts_ms": 1_700_000_000_000 + i * 1000})
    cells = {c.contract_type: c for c in analyse_cells(
        load_proposals(tmp_path),
        {("frxEURUSD", 300): outcomes(tie=40, up=480, down=480)}, LOOSE)}
    # Both variants must be stressed AWAY from their central estimate.
    assert cells["CALL"].required_edge_hi > cells["CALL"].required_edge
    assert cells["CALLE"].required_edge_hi > cells["CALLE"].required_edge


def test_a_non_positive_required_edge_withholds_the_verdict(tmp_path):
    """Free money with no skill means the measurement is wrong, not that the
    trade is good. The report must not print GO."""
    seed(tmp_path, b=0.95, n=60, ct="CALLE")
    with CensusStore(tmp_path) as store:
        for i in range(60):
            store.write(PROPOSALS, {
                "symbol": "frxEURUSD", "contract_type": "CALLE",
                "variant": "equals", "duration_s": 300, "b": 0.95,
                "sub_id": "e", "ts_ms": 1_700_000_000_000 + i * 1000})
    report = build_report(tmp_path, LOOSE)
    best = report.cells[0]
    if best.required_edge <= 0:
        assert report.overall_verdict == INSUFFICIENT
        assert "measurement fault" in report.rationale


def test_implausible_cells_carry_no_tradeable_verdict(tmp_path):
    """The per-cell column must agree with the headline: a cell that appears
    to profit with zero skill is a measurement fault, not a GO."""
    seed(tmp_path, b=0.99, n=60, ct="CALLE")
    cells = analyse_cells(
        load_proposals(tmp_path),
        {("frxEURUSD", 300): outcomes(tie=200, up=400, down=400)}, LOOSE)
    assert cells[0].required_edge <= 0
    assert cells[0].verdict == INSUFFICIENT
    assert any("non-positive" in n for n in cells[0].notes)
