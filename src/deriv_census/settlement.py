"""Empirical settlement outcomes measured from the recorded tick stream.

This module answers: for symbol S and duration T, how often does the exit
quote finish above, below, or exactly equal to the entry quote?

Why measure rather than model
-----------------------------
Modelling the tie rate from a normal approximation gives roughly
``phi(0) / sigma_ticks``, which at a one-minute horizon on a major FX pair
lands somewhere near 3%. That is the same order of magnitude as the entire
directional edge a good model can produce, so the difference between a modelled
3% and an actual 1% or 7% changes the verdict. It has to be measured, on the
venue's own quote feed, at the venue's own quote granularity.

Matching Deriv's settlement convention
--------------------------------------
A Rise/Fall contract compares the exit tick against the ENTRY TICK, which is
the first tick after the contract starts -- not the quote visible at decision
time. This module reproduces that convention exactly: entry is a tick, exit is
the first tick at or after ``entry_epoch + duration``.

The same subtlety is a live trap for the strategy itself. A backtest labelled
off the decision-time price instead of the entry tick quietly awards itself the
first fraction of a second of every move, which correlates with signal strength
and inflates measured edge. The bias survives a look-ahead audit, because no
future data is used. Measuring the convention here is the first defence.

Overlapping windows
-------------------
Consecutive entry ticks produce heavily overlapping, autocorrelated samples.
They are unbiased for the point estimate, so all of them are used for it, but
they badly overstate the information content. Confidence intervals are
therefore computed from a non-overlapping subsample, spaced one full duration
apart. Both counts are reported so the difference is visible rather than
buried.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .stats import wilson_interval

#: A settlement sample is discarded when the exit tick arrives more than this
#: many seconds after the target, since the feed evidently had a gap.
DEFAULT_MAX_EXIT_LAG_S = 30.0


@dataclass(frozen=True)
class SettlementOutcomes:
    symbol: str
    duration_seconds: int
    up: int
    down: int
    tie: int
    #: Non-overlapping subsample, used for honest interval estimation.
    up_indep: int
    down_indep: int
    tie_indep: int
    discarded_gap: int

    @property
    def total(self) -> int:
        return self.up + self.down + self.tie

    @property
    def total_indep(self) -> int:
        return self.up_indep + self.down_indep + self.tie_indep

    @property
    def tie_rate(self) -> float:
        return self.tie / self.total if self.total else math.nan

    @property
    def up_rate(self) -> float:
        return self.up / self.total if self.total else math.nan

    @property
    def drift(self) -> float:
        """Directional imbalance among non-tie outcomes, ``P(up) - 0.5``.

        Expected to be indistinguishable from zero. A persistently non-zero
        value is far more likely to indicate a feed artefact than a tradeable
        drift, and should be investigated before it is believed.
        """
        non_tie = self.up + self.down
        return (self.up / non_tie - 0.5) if non_tie else math.nan

    def tie_rate_interval(self, confidence: float = 0.95) -> tuple[float, float]:
        """Wilson interval computed on the non-overlapping subsample."""
        if not self.total_indep:
            return (math.nan, math.nan)
        return wilson_interval(self.tie_indep, self.total_indep, confidence)

    def as_dict(self) -> dict[str, float | int | str]:
        lo, hi = self.tie_rate_interval()
        return {
            "symbol": self.symbol,
            "duration_s": self.duration_seconds,
            "samples": self.total,
            "samples_independent": self.total_indep,
            "up": self.up, "down": self.down, "tie": self.tie,
            "tie_rate": self.tie_rate,
            "tie_rate_lo": lo, "tie_rate_hi": hi,
            "up_rate": self.up_rate,
            "drift": self.drift,
            "discarded_gap": self.discarded_gap,
        }


def quantise(quotes: np.ndarray, decimals: int | None) -> np.ndarray:
    """Round quotes to the feed's own precision before comparing.

    Comparing parsed floats directly makes ties essentially impossible to
    observe -- 1.10345 and 1.10345 can differ in the last bit after a JSON
    round trip -- which would silently report a tie rate of zero and flip the
    verdict. Rounding to the pip precision is what makes the comparison mean
    what Deriv means by it.
    """
    if decimals is None:
        return quotes
    return np.round(quotes, decimals)


def pip_decimals(pip_size: float | None) -> int | None:
    if not pip_size or pip_size <= 0:
        return None
    return max(0, int(round(-math.log10(pip_size))))


def settlement_outcomes(
    epochs: np.ndarray,
    quotes: np.ndarray,
    duration_seconds: int,
    symbol: str = "",
    decimals: int | None = None,
    max_exit_lag_s: float = DEFAULT_MAX_EXIT_LAG_S,
) -> SettlementOutcomes:
    """Classify every entry tick's outcome at ``duration_seconds``.

    ``epochs`` and ``quotes`` must be sorted by time and equal in length.
    """
    if epochs.shape != quotes.shape:
        raise ValueError("epochs and quotes must have the same shape")
    if epochs.size < 2:
        return SettlementOutcomes(symbol, duration_seconds, 0, 0, 0, 0, 0, 0, 0)
    if not np.all(np.diff(epochs) >= 0):
        order = np.argsort(epochs, kind="stable")
        epochs, quotes = epochs[order], quotes[order]

    q = quantise(np.asarray(quotes, dtype=float), decimals)
    t = np.asarray(epochs, dtype=float)

    # For each entry i, the exit is the first tick at or after t[i] + duration.
    targets = t + float(duration_seconds)
    exit_idx = np.searchsorted(t, targets, side="left")

    valid = exit_idx < t.size
    discarded = int((~valid).sum())

    entry_idx = np.nonzero(valid)[0]
    exit_idx = exit_idx[valid]

    # Drop samples where the feed gapped over the settlement instant.
    lag = t[exit_idx] - targets[entry_idx]
    within = lag <= max_exit_lag_s
    discarded += int((~within).sum())
    entry_idx, exit_idx = entry_idx[within], exit_idx[within]

    if entry_idx.size == 0:
        return SettlementOutcomes(symbol, duration_seconds, 0, 0, 0, 0, 0, 0,
                                  discarded)

    delta = q[exit_idx] - q[entry_idx]
    up = int((delta > 0).sum())
    down = int((delta < 0).sum())
    tie = int((delta == 0).sum())

    # Non-overlapping subsample: greedily take entries spaced at least one
    # full duration apart, so no two samples share any price path.
    keep: list[int] = []
    next_allowed = -np.inf
    for pos in range(entry_idx.size):
        entry_time = t[entry_idx[pos]]
        if entry_time >= next_allowed:
            keep.append(pos)
            next_allowed = entry_time + float(duration_seconds)
    sel = np.asarray(keep, dtype=int)
    d_indep = delta[sel]

    return SettlementOutcomes(
        symbol=symbol,
        duration_seconds=duration_seconds,
        up=up, down=down, tie=tie,
        up_indep=int((d_indep > 0).sum()),
        down_indep=int((d_indep < 0).sum()),
        tie_indep=int((d_indep == 0).sum()),
        discarded_gap=discarded,
    )
