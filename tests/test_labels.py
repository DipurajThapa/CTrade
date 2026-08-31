"""Labels must match how the contract settles, not how the backtest wishes."""

import numpy as np
import pytest

from deriv_census.labels import (DOWN, TIE, UP, build_labels,
                                 non_overlapping_mask)


def series(n=2000, step=0.0, sigma=0.0, seed=1, start=1.10000):
    epochs = np.arange(float(n))
    rng = np.random.default_rng(seed)
    moves = np.full(n, step) + rng.normal(0, sigma, n) if sigma else np.full(n, step)
    return epochs, np.round(start + np.cumsum(moves), 5)


def test_entry_is_the_first_tick_after_decision_plus_latency():
    epochs, quotes = series(step=1e-05)
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=3.0, decimals=5)
    assert len(labels) > 0
    lag = epochs[labels.entry_index] - epochs[labels.decision_index]
    assert lag.min() > 3.0          # strictly after, never at, decision + latency
    assert lag.max() <= 5.0


def test_zero_latency_still_enters_on_the_next_tick_not_the_current_one():
    """Even with no latency the contract enters on a later tick; a decision
    cannot be filled at the price that triggered it."""
    epochs, quotes = series(step=1e-05)
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=0.0, decimals=5)
    assert bool((labels.entry_index > labels.decision_index).all())


def test_decision_time_labelling_inflates_the_measured_move():
    """The bias this module exists to prevent.

    Under a rising series, labelling from the decision-time price credits the
    strategy with the movement that happens before it is actually filled.
    """
    epochs, quotes = series(step=2e-05)
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=1.5, decimals=5)

    honest = labels.exit_quote - labels.entry_quote
    optimistic = labels.exit_quote - quotes[labels.decision_index]

    assert optimistic.mean() > honest.mean()
    # The gap is exactly the pre-fill movement handed over for free.
    handed_over = quotes[labels.entry_index] - quotes[labels.decision_index]
    assert np.allclose(optimistic - honest, handed_over)
    assert handed_over.mean() > 0


def test_latency_can_be_sampled_from_a_measured_distribution():
    """Latency is right-skewed; collapsing it to a mean models fills that
    never happen."""
    epochs, quotes = series(step=1e-05)
    measured = np.array([0.3, 0.4, 0.5, 0.6, 4.0, 9.0])   # skewed tail
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=measured,
                          decimals=5, rng=np.random.default_rng(7))
    lag = epochs[labels.entry_index] - epochs[labels.decision_index]
    assert lag.min() < 2.0 and lag.max() > 4.0      # the tail is represented
    assert len(np.unique(lag)) > 1


def test_direction_labels_are_exact():
    """Traced by hand against the settlement convention.

        t:  0    1    2    61   62   63
        q:  1.0  2.0  3.0  2.0  2.0  2.0

    decision 0 -> entry t=1 (2.0), exit t=61 (2.0)          -> TIE
    decision 1 -> entry t=2 (3.0), exit t=62 (2.0)          -> DOWN
    decision 2 -> entry t=61, 59s after the decision        -> dropped, stale
    decisions 3-5 -> exit would fall beyond the data        -> dropped
    """
    epochs = np.array([0.0, 1.0, 2.0, 61.0, 62.0, 63.0])
    quotes = np.array([1.0, 2.0, 3.0, 2.0, 2.0, 2.0])
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=0.0, decimals=0)
    assert labels.decision_index.tolist() == [0, 1]
    assert labels.label.tolist() == [TIE, DOWN]
    assert labels.entry_quote.tolist() == [2.0, 3.0]
    assert labels.exit_quote.tolist() == [2.0, 2.0]


def test_a_decision_whose_fill_lands_after_a_feed_gap_is_dropped():
    """Live, the data-staleness veto refuses to trade on a stale quote, so
    labelling such a decision would train on a trade that never happens."""
    epochs = np.array([0.0, 90.0, 150.0, 210.0])
    quotes = np.array([1.0, 2.0, 3.0, 4.0])
    assert len(build_labels(epochs, quotes, horizon_s=60, latency_s=0.0,
                            decimals=0, max_entry_lag_s=5.0)) == 0
    # Permit the gap and the same decision reappears.
    assert len(build_labels(epochs, quotes, horizon_s=60, latency_s=0.0,
                            decimals=0, max_entry_lag_s=120.0)) > 0


def test_ties_are_detected_at_the_pip_grid():
    epochs, quotes = series(step=0.0)
    quotes = quotes + np.linspace(0, 1e-12, quotes.size)   # sub-pip float noise
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=0.0, decimals=5)
    assert labels.tie_rate == pytest.approx(1.0)
    assert (labels.label == TIE).all()


def test_forward_return_matches_entry_and_exit_quotes():
    epochs, quotes = series(step=1e-05, sigma=1e-05)
    labels = build_labels(epochs, quotes, horizon_s=120, latency_s=1.0, decimals=5)
    assert np.allclose(labels.forward_return,
                       labels.exit_quote - labels.entry_quote)
    assert np.all(np.sign(labels.forward_return) == np.where(
        labels.label == TIE, 0, labels.label))


def test_feed_gaps_are_excluded_rather_than_settled_late():
    epochs = np.array([0.0, 1.0, 500.0, 501.0])
    quotes = np.array([1.0, 1.0, 2.0, 2.0])
    labels = build_labels(epochs, quotes, horizon_s=60, latency_s=0.0,
                          decimals=5, max_exit_lag_s=30.0)
    assert len(labels) == 0


def test_non_overlapping_mask_spaces_samples_by_a_full_horizon():
    epochs, quotes = series(n=3600, sigma=1e-05)
    labels = build_labels(epochs, quotes, horizon_s=300, latency_s=0.0,
                          decimals=5)
    mask = non_overlapping_mask(epochs, labels.decision_index, 300)
    kept = epochs[labels.decision_index[mask]]
    assert np.all(np.diff(kept) >= 300)
    assert mask.sum() < len(labels) / 100      # far fewer than the raw count


def test_degenerate_inputs():
    empty = build_labels(np.array([]), np.array([]), horizon_s=60)
    assert len(empty) == 0
    assert np.isnan(empty.tie_rate)
    with pytest.raises(ValueError):
        build_labels(np.array([1.0, 2.0]), np.array([1.0]), horizon_s=60)
    with pytest.raises(ValueError):
        build_labels(*series(n=10), horizon_s=0)
