import numpy as np
import pytest

from deriv_census.settlement import (pip_decimals, quantise,
                                     settlement_outcomes)


def test_exit_is_the_first_tick_at_or_after_the_duration():
    """Deriv settles against the tick at/after expiry, not the nearest one."""
    epochs = np.array([0.0, 10.0, 59.0, 61.0, 200.0])
    quotes = np.array([1.10000, 1.10005, 1.10009, 1.10020, 1.10030])
    out = settlement_outcomes(epochs, quotes, 60, decimals=5)
    # Entry at t=0 must settle against t=61 (1.10020), not t=59.
    assert out.up >= 1
    assert out.total > 0


def test_ties_are_counted_at_the_pip_grid_not_at_float_precision():
    epochs = np.arange(0.0, 100.0)
    quotes = np.full(100, 1.10000)
    quotes[50:] = 1.10000 + 1e-12   # sub-pip noise must NOT break the tie
    out = settlement_outcomes(epochs, quotes, 10, decimals=5)
    assert out.tie == out.total
    assert out.tie_rate == pytest.approx(1.0)


def test_without_quantisation_float_noise_would_destroy_the_tie_signal():
    epochs = np.arange(0.0, 100.0)
    quotes = np.full(100, 1.10000) + np.linspace(0, 1e-12, 100)
    unquantised = settlement_outcomes(epochs, quotes, 10, decimals=None)
    quantised = settlement_outcomes(epochs, quotes, 10, decimals=5)
    assert unquantised.tie_rate < quantised.tie_rate
    assert quantised.tie_rate == pytest.approx(1.0)


def test_directional_counts_are_exact():
    epochs = np.array([0.0, 1.0, 2.0, 3.0, 10.0, 11.0, 12.0, 13.0])
    quotes = np.array([1.0, 2.0, 3.0, 4.0, 2.0, 2.0, 2.0, 2.0])
    out = settlement_outcomes(epochs, quotes, 10, decimals=0)
    # entries 0..3 (values 1,2,3,4) settle against 2.0 at t=10.
    assert out.up == 1 and out.tie == 1 and out.down == 2
    assert out.total == 4


def test_samples_beyond_the_data_are_discarded_not_counted():
    epochs = np.arange(0.0, 10.0)
    quotes = np.linspace(1.0, 1.1, 10)
    out = settlement_outcomes(epochs, quotes, 5, decimals=5)
    assert out.discarded_gap == 5      # last five entries have no exit tick
    assert out.total == 5


def test_feed_gaps_are_excluded():
    """A quote gap spanning the settlement instant is not a settlement."""
    epochs = np.array([0.0, 1.0, 500.0, 501.0])
    quotes = np.array([1.0, 1.0, 2.0, 2.0])
    out = settlement_outcomes(epochs, quotes, 60, decimals=5,
                              max_exit_lag_s=30.0)
    assert out.total == 0
    assert out.discarded_gap == 4


def test_independent_subsample_is_non_overlapping_and_much_smaller():
    epochs = np.arange(0.0, 3600.0)
    rng = np.random.default_rng(3)
    quotes = 1.1 + np.cumsum(rng.normal(0, 1e-5, epochs.size))
    out = settlement_outcomes(epochs, quotes, 300, decimals=5)
    assert out.total > 3000
    # One sample per 300s window, so roughly 3600/300.
    assert out.total_indep <= 13
    lo, hi = out.tie_rate_interval()
    assert lo <= out.tie_rate <= hi or out.tie == 0


def test_overlapping_samples_would_overstate_confidence():
    epochs = np.arange(0.0, 3600.0)
    rng = np.random.default_rng(5)
    quotes = 1.1 + np.cumsum(rng.normal(0, 2e-5, epochs.size))
    out = settlement_outcomes(epochs, quotes, 60, decimals=5)
    honest = out.tie_rate_interval()
    from deriv_census.stats import wilson_interval
    naive = wilson_interval(out.tie, out.total)
    assert (honest[1] - honest[0]) > (naive[1] - naive[0])


def test_unsorted_input_is_sorted_rather_than_silently_wrong():
    epochs = np.array([10.0, 0.0, 20.0, 30.0])
    quotes = np.array([2.0, 1.0, 3.0, 4.0])
    out = settlement_outcomes(epochs, quotes, 10, decimals=0)
    assert out.up == out.total and out.total == 3


def test_degenerate_inputs():
    empty = settlement_outcomes(np.array([]), np.array([]), 60)
    assert empty.total == 0
    assert np.isnan(empty.tie_rate)
    with pytest.raises(ValueError):
        settlement_outcomes(np.array([1.0, 2.0]), np.array([1.0]), 60)


def test_drift_is_near_zero_for_a_random_walk():
    rng = np.random.default_rng(17)
    epochs = np.arange(0.0, 60_000.0)
    quotes = 1.1 + np.cumsum(rng.normal(0, 1e-5, epochs.size))
    out = settlement_outcomes(epochs, quotes, 300, decimals=5)
    assert abs(out.drift) < 0.05


@pytest.mark.parametrize("pip,decimals", [
    (1e-05, 5), (0.001, 3), (0.01, 2), (1.0, 0), (None, None), (0, None)])
def test_pip_decimals(pip, decimals):
    assert pip_decimals(pip) == decimals


def test_quantise_is_identity_without_decimals():
    arr = np.array([1.123456789])
    assert quantise(arr, None)[0] == arr[0]
    assert quantise(arr, 3)[0] == pytest.approx(1.123)


def test_a_frozen_feed_is_degenerate_not_a_crash():
    """A market that is open but not quoting yields p_tie == 1.0. That must
    be reported as a data fault, never raise out of the analysis."""
    import math

    from deriv_census.stats import (required_edge,
                                    required_information_coefficient)

    epochs = np.arange(0.0, 1000.0)
    quotes = np.full(1000, 1.10000)
    out = settlement_outcomes(epochs, quotes, 60, decimals=5)
    assert out.tie_rate == 1.0
    assert required_edge(0.9, 1.0, "strict") == math.inf
    assert required_edge(0.9, 1.0, "equals") == -math.inf
    assert required_information_coefficient(0.9, 1.0, "strict") == math.inf
    assert required_information_coefficient(0.9, 1.0, "equals") == -math.inf
