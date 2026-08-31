"""Forward labels that match how a binary contract actually settles.

The label a research pipeline uses decides what every downstream number means,
and the obvious definition is wrong in a way that inflates measured edge.

A Rise/Fall contract compares the exit tick against the ENTRY TICK -- the first
tick after the contract starts -- not against the quote visible when the
decision was made. Between decision and entry sits your latency: serialise,
network, venue accept, next tick. Realistically 300ms to 1.5s.

That window is not empty. It contains exactly the price movement the signal is
predicting, and its content is positively correlated with signal strength. A
backtest labelled from the decision-time price therefore awards itself the
opening fraction of every move for free.

The bias survives a look-ahead audit, because no future data is consulted. It
is not leakage; it is an entry-point misspecification. The only defence is to
label from the entry tick, with latency sampled from its measured distribution
rather than assumed constant -- latency is right-skewed, and the tail is where
fills are worst.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

UP, DOWN, TIE, UNDEFINED = 1, -1, 0, -9


@dataclass(frozen=True)
class LabelSet:
    """Forward outcomes aligned to the decision index."""

    decision_index: np.ndarray   # index into the tick series where a decision is made
    entry_index: np.ndarray      # tick actually used as the contract entry
    exit_index: np.ndarray       # tick at or after entry + horizon
    entry_quote: np.ndarray
    exit_quote: np.ndarray
    label: np.ndarray            # UP / DOWN / TIE

    def __len__(self) -> int:
        return int(self.decision_index.size)

    @property
    def tie_rate(self) -> float:
        return float((self.label == TIE).mean()) if len(self) else float("nan")

    @property
    def forward_return(self) -> np.ndarray:
        """Signed move from entry to exit, in price units."""
        return self.exit_quote - self.entry_quote

    def non_tie_mask(self) -> np.ndarray:
        return self.label != TIE


def build_labels(
    epochs: np.ndarray,
    quotes: np.ndarray,
    horizon_s: float,
    latency_s: np.ndarray | float = 0.0,
    decimals: int | None = None,
    max_entry_lag_s: float = 5.0,
    max_exit_lag_s: float = 30.0,
    rng: np.random.Generator | None = None,
) -> LabelSet:
    """Label every decision point using the venue's settlement convention.

    ``latency_s`` may be a scalar or an empirical sample of measured latencies.
    When it is a sample, a latency is drawn per decision, which propagates the
    real right-skewed distribution into the labels instead of flattening it to
    a mean that no fill ever experiences.

    ``max_entry_lag_s`` drops decisions whose entry tick arrives long after the
    decision, which happens when the feed gaps. Live, such a trade would never
    be placed: the data-staleness veto refuses to act on a stale quote. Keeping
    those labels would train on decisions the running system would have
    declined, and on a sixty-second horizon an entry filled fifty seconds late
    is not the same trade at all.
    """
    epochs = np.asarray(epochs, dtype=float)
    quotes = np.asarray(quotes, dtype=float)
    if epochs.shape != quotes.shape:
        raise ValueError("epochs and quotes must have the same shape")
    if epochs.size < 2:
        empty_i = np.empty(0, dtype=int)
        empty_f = np.empty(0, dtype=float)
        return LabelSet(empty_i, empty_i, empty_i, empty_f, empty_f, empty_i)
    if horizon_s <= 0:
        raise ValueError("horizon_s must be positive")

    if decimals is not None:
        quotes = np.round(quotes, decimals)

    n = epochs.size
    decision = np.arange(n)

    if np.isscalar(latency_s):
        lat = np.full(n, float(latency_s))
    else:
        sample = np.asarray(latency_s, dtype=float)
        if sample.size == 0:
            lat = np.zeros(n)
        else:
            generator = rng or np.random.default_rng(20260101)
            lat = generator.choice(sample, size=n, replace=True)

    # Entry is the first tick strictly after the decision plus latency.
    requested = epochs + lat
    entry = np.searchsorted(epochs, requested, side="right")
    valid = entry < n
    decision, entry, requested = decision[valid], entry[valid], requested[valid]

    # Reject fills the feed gapped into; live these decisions never happen.
    fresh = (epochs[entry] - requested) <= max_entry_lag_s
    decision, entry = decision[fresh], entry[fresh]

    # Exit is the first tick at or after entry + horizon.
    targets = epochs[entry] + horizon_s
    exit_ = np.searchsorted(epochs, targets, side="left")
    valid = exit_ < n
    decision, entry, exit_, targets = (decision[valid], entry[valid],
                                       exit_[valid], targets[valid])

    # Discard settlements the feed gapped over.
    within = (epochs[exit_] - targets) <= max_exit_lag_s
    decision, entry, exit_ = decision[within], entry[within], exit_[within]

    entry_q, exit_q = quotes[entry], quotes[exit_]
    delta = exit_q - entry_q
    label = np.where(delta > 0, UP, np.where(delta < 0, DOWN, TIE))

    return LabelSet(decision, entry, exit_, entry_q, exit_q, label)


def non_overlapping_mask(epochs: np.ndarray, decision_index: np.ndarray,
                         horizon_s: float) -> np.ndarray:
    """Select decisions spaced at least one horizon apart.

    Consecutive decisions share almost all of their price path, so treating
    them as independent overstates the information in a sample by orders of
    magnitude. Point estimates use everything; every significance claim uses
    this subset.
    """
    keep = np.zeros(decision_index.size, dtype=bool)
    next_allowed = -np.inf
    for position, index in enumerate(decision_index):
        t = epochs[index]
        if t >= next_allowed:
            keep[position] = True
            next_allowed = t + horizon_s
    return keep
