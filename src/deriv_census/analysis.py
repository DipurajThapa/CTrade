"""Turn captured records into per-cell economics and a pre-registered verdict.

The output is deliberately narrow: for every (symbol, contract type, duration)
cell, the directional edge a model would need just to break even, and whether
that number is inside what a good model can actually deliver.

Everything else -- payout quantiles, drift, tie rates, session breakdowns -- is
diagnostic detail supporting that one number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import DecisionConfig
from .protocol import VARIANT_OF_CONTRACT_TYPE
from .settlement import SettlementOutcomes, pip_decimals, settlement_outcomes
from .stats import (bootstrap_median_interval, evaluate,
                    required_edge, required_information_coefficient)
from .storage import PROPOSALS, TICKS, read_stream

GO = "GO"
CONDITIONAL = "CONDITIONAL"
STOP = "STOP"
INSUFFICIENT = "INSUFFICIENT_DATA"

#: London/New York overlap in UTC. Liquidity is highest here, which is where
#: ties are rarest and payouts are most likely to be competitive, so the census
#: reports this window separately from the twenty-four-hour aggregate.
OVERLAP_UTC = (12, 16)


def load_proposals(root: str | Path) -> pd.DataFrame:
    rows = list(read_stream(root, PROPOSALS))
    if not rows:
        return pd.DataFrame(columns=["symbol", "contract_type", "duration_s",
                                     "b", "ts_ms", "sub_id"])
    frame = pd.DataFrame(rows)
    frame = frame[frame["b"].notna()].copy()
    frame["b"] = pd.to_numeric(frame["b"], errors="coerce")
    frame = frame[frame["b"].notna()]
    # A payout fraction outside this band is a parsing failure, not a quote.
    frame = frame[(frame["b"] > -1.0) & (frame["b"] < 10.0)]
    frame["ts_ms"] = pd.to_numeric(frame["ts_ms"], errors="coerce")
    frame["hour_utc"] = ((frame["ts_ms"] // 3_600_000) % 24).astype("Int64")
    if "variant" not in frame.columns:
        frame["variant"] = frame["contract_type"].map(VARIANT_OF_CONTRACT_TYPE)
    return frame


def load_ticks(root: str | Path) -> pd.DataFrame:
    rows = list(read_stream(root, TICKS))
    if not rows:
        return pd.DataFrame(columns=["symbol", "tick_epoch", "quote", "pip_size"])
    frame = pd.DataFrame(rows)
    for col in ("tick_epoch", "quote", "pip_size"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["tick_epoch", "quote"])
    # The same tick can arrive twice across a reconnect.
    return frame.drop_duplicates(subset=["symbol", "tick_epoch"]).sort_values(
        ["symbol", "tick_epoch"])


def measure_settlement(ticks: pd.DataFrame, durations: Iterable[int]
                       ) -> dict[tuple[str, int], SettlementOutcomes]:
    out: dict[tuple[str, int], SettlementOutcomes] = {}
    if ticks.empty:
        return out
    for symbol, group in ticks.groupby("symbol", sort=True):
        pip = group["pip_size"].dropna()
        decimals = pip_decimals(float(pip.iloc[0])) if not pip.empty else None
        epochs = group["tick_epoch"].to_numpy(dtype=float)
        quotes = group["quote"].to_numpy(dtype=float)
        for duration in durations:
            out[(str(symbol), int(duration))] = settlement_outcomes(
                epochs, quotes, int(duration), symbol=str(symbol),
                decimals=decimals)
    return out


def payout_drift(frame: pd.DataFrame) -> dict[str, float]:
    """How far the quoted payout moves between consecutive quotes on one cell.

    Two consequences for the live system. It sets the floor on the edge buffer,
    because a quote captured at decision time can be worse by this much at
    submit. And it is the size of the option in holding a candidate that fails
    on payout alone: if drift is material, re-requesting within the signal's
    decay window converts payout volatility from a cost into a free option.
    """
    if frame.empty or "sub_id" not in frame.columns:
        return {"n": 0}
    deltas: list[float] = []
    for _, group in frame.dropna(subset=["sub_id"]).groupby(
            ["sub_id"], sort=False):
        values = group.sort_values("ts_ms")["b"].to_numpy(dtype=float)
        if values.size >= 2:
            deltas.extend(np.abs(np.diff(values)).tolist())
    if not deltas:
        return {"n": 0}
    arr = np.asarray(deltas, dtype=float)
    return {
        "n": int(arr.size),
        "mean_abs": float(arr.mean()),
        "p50": float(np.quantile(arr, 0.50)),
        "p95": float(np.quantile(arr, 0.95)),
        "max": float(arr.max()),
        "share_nonzero": float((arr > 0).mean()),
    }


@dataclass
class CellResult:
    symbol: str
    contract_type: str
    variant: str
    duration_s: int
    n_proposals: int
    b_median: float
    b_p10: float
    b_p90: float
    b_ci: tuple[float, float]
    b_median_overlap: float
    tie_rate: float
    tie_ci: tuple[float, float]
    settlement_samples: int
    settlement_samples_indep: int
    breakeven_probability: float
    house_margin: float
    required_edge: float
    required_edge_hi: float
    required_ic: float
    verdict: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "contract_type": self.contract_type,
            "variant": self.variant,
            "duration_s": self.duration_s,
            "n_proposals": self.n_proposals,
            "b_median": self.b_median,
            "b_p10": self.b_p10,
            "b_p90": self.b_p90,
            "b_ci_lo": self.b_ci[0], "b_ci_hi": self.b_ci[1],
            "b_median_overlap_session": self.b_median_overlap,
            "tie_rate": self.tie_rate,
            "tie_ci_lo": self.tie_ci[0], "tie_ci_hi": self.tie_ci[1],
            "settlement_samples": self.settlement_samples,
            "settlement_samples_indep": self.settlement_samples_indep,
            "breakeven_probability": self.breakeven_probability,
            "house_margin": self.house_margin,
            "required_edge": self.required_edge,
            "required_edge_hi": self.required_edge_hi,
            "required_ic": self.required_ic,
            "verdict": self.verdict,
            "notes": "; ".join(self.notes),
        }


def classify(required: float, decision: DecisionConfig) -> str:
    if not math.isfinite(required):
        return STOP
    if required <= decision.go_max_required_edge:
        return GO
    if required <= decision.conditional_max_required_edge:
        return CONDITIONAL
    return STOP


def analyse_cells(proposals: pd.DataFrame,
                  settlement: dict[tuple[str, int], SettlementOutcomes],
                  decision: DecisionConfig) -> list[CellResult]:
    results: list[CellResult] = []
    if proposals.empty:
        return results

    for (symbol, contract_type, duration), group in proposals.groupby(
            ["symbol", "contract_type", "duration_s"], sort=True):
        duration = int(duration)
        values = group["b"].to_numpy(dtype=float)
        variant = VARIANT_OF_CONTRACT_TYPE.get(str(contract_type), "strict")
        notes: list[str] = []

        outcome = settlement.get((str(symbol), duration))
        if outcome is None or outcome.total == 0:
            tie_rate, tie_ci = 0.0, (0.0, 0.0)
            n_set = n_set_indep = 0
            notes.append("no tick coverage; tie rate assumed zero, which makes "
                         "the required edge an OPTIMISTIC lower bound")
        else:
            tie_rate = outcome.tie_rate
            tie_ci = outcome.tie_rate_interval()
            n_set, n_set_indep = outcome.total, outcome.total_indep

        b_median = float(np.median(values))
        econ = evaluate(b_median, tie_rate, variant)

        # Adverse case: the tenth-percentile payout combined with whichever
        # end of the tie interval hurts THIS variant. Ties penalise strict
        # contracts and subsidise equals contracts, so the adverse tie bound
        # is the upper one for strict and the lower one for equals. Using the
        # upper bound for both would report a fantastically favourable number
        # as though it were a stress case.
        adverse_tie = tie_ci[1] if variant == "strict" else tie_ci[0]
        if not math.isfinite(adverse_tie):
            adverse_tie = tie_rate
        b_p10 = float(np.quantile(values, 0.10))
        try:
            required_hi = required_edge(b_p10, adverse_tie, variant)
        except ValueError:
            required_hi = math.inf

        overlap = group[(group["hour_utc"] >= OVERLAP_UTC[0])
                        & (group["hour_utc"] < OVERLAP_UTC[1])]
        b_overlap = (float(np.median(overlap["b"].to_numpy(dtype=float)))
                     if len(overlap) else math.nan)

        if tie_rate >= 1.0:
            notes.append(
                "every settlement sampled was a tie: the feed did not move "
                "over this horizon. This is a data fault, not a tradeable "
                "condition -- check whether the market was quoting.")

        if econ.required_edge <= 0.0:
            notes.append(
                "required edge is non-positive: at this payout the tie rate "
                "alone makes the contract profitable with no directional "
                "skill. Treat as a measurement fault until reproduced -- a "
                "venue that mispriced its own tie convention this far would "
                "be unusual. Verify the tie rate against raw ticks and "
                "confirm the payout is for the equals variant.")

        if econ.required_edge <= 0.0 or tie_rate >= 1.0:
            # Flagged above as a measurement fault. A cell that appears to
            # profit with no skill must not carry a tradeable verdict.
            verdict = INSUFFICIENT
        elif len(values) < decision.min_proposals_per_cell:
            verdict = INSUFFICIENT
            notes.append(f"only {len(values)} proposals "
                         f"(need {decision.min_proposals_per_cell})")
        elif n_set and n_set < decision.min_settlement_samples_per_cell:
            verdict = INSUFFICIENT
            notes.append(f"only {n_set} settlement samples "
                         f"(need {decision.min_settlement_samples_per_cell})")
        else:
            verdict = classify(econ.required_edge, decision)

        results.append(CellResult(
            symbol=str(symbol), contract_type=str(contract_type),
            variant=variant, duration_s=duration,
            n_proposals=int(values.size),
            b_median=b_median,
            b_p10=b_p10,
            b_p90=float(np.quantile(values, 0.90)),
            b_ci=bootstrap_median_interval(values),
            b_median_overlap=b_overlap,
            tie_rate=tie_rate, tie_ci=tie_ci,
            settlement_samples=n_set, settlement_samples_indep=n_set_indep,
            breakeven_probability=econ.breakeven_probability,
            house_margin=econ.house_margin,
            required_edge=econ.required_edge,
            required_edge_hi=required_hi,
            required_ic=econ.required_ic,
            verdict=verdict, notes=notes))

    results.sort(key=lambda r: (not math.isfinite(r.required_edge),
                                r.required_edge))
    return results


@dataclass
class CensusReport:
    generated_at: str
    coverage: dict[str, Any]
    drift: dict[str, float]
    cells: list[CellResult]
    overall_verdict: str
    rationale: str
    decision: DecisionConfig

    def best(self) -> CellResult | None:
        eligible = [c for c in self.cells if c.verdict != INSUFFICIENT]
        return eligible[0] if eligible else None

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([c.as_dict() for c in self.cells])


def coverage_summary(proposals: pd.DataFrame, ticks: pd.DataFrame
                     ) -> dict[str, Any]:
    def span(series: pd.Series, scale: float) -> float:
        if series.empty:
            return 0.0
        return float((series.max() - series.min()) / scale)

    return {
        "proposal_records": int(len(proposals)),
        "tick_records": int(len(ticks)),
        "symbols_quoted": int(proposals["symbol"].nunique()) if len(proposals) else 0,
        "symbols_ticked": int(ticks["symbol"].nunique()) if len(ticks) else 0,
        "cells": int(proposals.groupby(
            ["symbol", "contract_type", "duration_s"]).ngroups) if len(proposals) else 0,
        "proposal_span_hours": round(
            span(proposals["ts_ms"], 3_600_000.0) if len(proposals) else 0.0, 2),
        "tick_span_hours": round(
            span(ticks["tick_epoch"], 3600.0) if len(ticks) else 0.0, 2),
    }


def build_report(root: str | Path, decision: DecisionConfig,
                 durations: Iterable[int] | None = None) -> CensusReport:
    proposals = load_proposals(root)
    ticks = load_ticks(root)

    if durations is None:
        durations = (sorted({int(d) for d in proposals["duration_s"].dropna()})
                     if len(proposals) else [])

    settlement = measure_settlement(ticks, durations)
    cells = analyse_cells(proposals, settlement, decision)
    coverage = coverage_summary(proposals, ticks)
    drift = payout_drift(proposals)

    eligible = [c for c in cells if c.verdict != INSUFFICIENT]
    if not eligible:
        overall, rationale = INSUFFICIENT, (
            "No cell reached the pre-registered minimum sample size. The "
            "census has not yet produced a decision; continue capture.")
    else:
        best = eligible[0]
        overall = best.verdict
        no_ticks = any("no tick coverage" in n for n in best.notes)
        implausible = best.required_edge <= 0.0 or best.tie_rate >= 1.0
        rationale = (
            f"Best cell {best.symbol} {best.contract_type} {best.duration_s}s: "
            f"median payout {best.b_median:.4f}, break-even "
            f"{best.breakeven_probability:.2%}, house margin "
            f"{best.house_margin:.2%}, measured tie rate {best.tie_rate:.2%}, "
            f"required directional edge {best.required_edge:.2%} "
            f"(adverse case {best.required_edge_hi:.2%}), "
            f"required IC {best.required_ic:.3f}.")
        if no_ticks:
            rationale += (" WARNING: no tick coverage for this cell, so the "
                          "tie penalty is absent and the requirement is "
                          "understated.")
        if implausible:
            overall = INSUFFICIENT
            rationale += (" WARNING: the required edge is non-positive, which "
                          "would mean the contract profits with no directional "
                          "skill at all. A venue mispricing its own tie "
                          "convention by this much would be extraordinary, so "
                          "treat this as a measurement fault: verify the tie "
                          "rate against raw ticks and confirm the payout "
                          "belongs to the variant it is attributed to. No "
                          "verdict is issued until it is explained.")
        elif overall == GO:
            rationale += (" Inside the band a strong short-horizon model can "
                          "plausibly reach. Proceed to modelling, scoped to "
                          "this cell only.")
        elif overall == CONDITIONAL:
            rationale += (" At the top of what a strong model delivers. "
                          "Proceed only against a pre-registered IC target "
                          "and a hard kill gate.")
        else:
            rationale += (" Above what short-horizon FX models achieve out of "
                          "sample. No model wins at this payout; do not spend "
                          "the build.")

    return CensusReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        coverage=coverage, drift=drift, cells=cells,
        overall_verdict=overall, rationale=rationale, decision=decision)
