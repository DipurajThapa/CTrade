"""The economics must be exactly right; everything else depends on it."""

import math

import pytest
from scipy.stats import norm

from deriv_census.stats import (bootstrap_median_interval, breakeven_probability,
                                evaluate, expected_value, house_margin,
                                kelly_fraction, payout_fraction, required_edge,
                                required_information_coefficient,
                                win_probability, wilson_interval)


def test_payout_fraction_is_net_of_stake():
    # Deriv quotes payout gross, so $10 -> $19.50 is b = 0.95, not 1.95.
    assert payout_fraction(10.0, 19.5) == pytest.approx(0.95)
    assert payout_fraction(10.0, 10.0) == pytest.approx(0.0)


@pytest.mark.parametrize("stake,payout", [(0, 10), (-1, 10), (10, -1)])
def test_payout_fraction_rejects_nonsense(stake, payout):
    with pytest.raises(ValueError):
        payout_fraction(stake, payout)


@pytest.mark.parametrize("b,expected", [
    (0.30, 0.769231), (0.50, 0.666667), (0.70, 0.588235),
    (0.80, 0.555556), (0.85, 0.540541), (0.90, 0.526316),
    (0.95, 0.512821), (0.98, 0.505051), (1.00, 0.500000),
])
def test_breakeven_probability_known_values(b, expected):
    assert breakeven_probability(b) == pytest.approx(expected, abs=1e-6)


def test_breakeven_makes_expectancy_exactly_zero():
    for b in (0.3, 0.5, 0.8, 0.95):
        w = breakeven_probability(b)
        assert w * b - (1 - w) == pytest.approx(0.0, abs=1e-12)


def test_house_margin_is_zero_only_at_even_money():
    assert house_margin(1.0) == pytest.approx(0.0)
    assert house_margin(0.8) == pytest.approx(0.0555556, abs=1e-6)
    assert house_margin(1.5) < 0  # a payout above 1:1 would favour the trader


def test_required_edge_exceeds_margin_whenever_ties_occur():
    """The whole reason ties are measured rather than ignored."""
    b = 0.95
    assert required_edge(b, 0.0) == pytest.approx(house_margin(b), abs=1e-12)
    assert required_edge(b, 0.028) > house_margin(b) * 2
    # Monotone in the tie rate.
    previous = -1.0
    for tie in (0.0, 0.01, 0.03, 0.07, 0.15):
        current = required_edge(b, tie)
        assert current > previous
        previous = current


def test_required_edge_strict_versus_equals_is_the_real_tradeoff():
    """A higher strict payout and a lower equals payout can be equivalent."""
    strict = required_edge(0.95, 0.028, "strict")
    equals = required_edge(0.85, 0.028, "equals")
    assert strict == pytest.approx(0.02759, abs=1e-4)
    assert equals == pytest.approx(0.02731, abs=1e-4)
    # Close enough that only measurement can decide - which is the point.
    assert abs(strict - equals) < 0.002


def test_equals_variant_beats_strict_at_the_same_payout():
    for tie in (0.005, 0.03, 0.09):
        assert required_edge(0.9, tie, "equals") < required_edge(0.9, tie, "strict")


def test_required_edge_can_be_unattainable():
    # A punitive payout with frequent ties can exceed perfect foresight:
    # break-even needs P(win) > 1, which no forecaster can deliver.
    assert required_edge(0.10, 0.10) > 0.5
    assert required_information_coefficient(0.10, 0.10) == math.inf


def test_required_ic_round_trips_through_the_normal_quantile():
    for b in (0.6, 0.8, 0.95):
        for tie in (0.0, 0.02, 0.05):
            ic = required_information_coefficient(b, tie)
            assert norm.cdf(ic) == pytest.approx(0.5 + required_edge(b, tie),
                                                 abs=1e-9)


def test_edge_at_requirement_gives_zero_expectancy():
    """The definition that ties the whole module together."""
    for b in (0.5, 0.8, 0.95):
        for tie in (0.0, 0.01, 0.04):
            for variant in ("strict", "equals"):
                e = required_edge(b, tie, variant)
                assert expected_value(b, e, tie, variant) == pytest.approx(
                    0.0, abs=1e-12)


def test_expectancy_is_increasing_in_edge_and_payout():
    assert expected_value(0.9, 0.05, 0.02) > expected_value(0.9, 0.01, 0.02)
    assert expected_value(0.95, 0.03, 0.02) > expected_value(0.85, 0.03, 0.02)


def test_zero_edge_loses_exactly_the_margin_when_ties_cannot_happen():
    assert expected_value(0.8, 0.0, 0.0) == pytest.approx(-0.1, abs=1e-12)


def test_win_probability_is_bounded():
    assert win_probability(10.0, 0.02) <= 1.0
    assert win_probability(-10.0, 0.02) >= 0.0


def test_kelly_is_zero_without_an_edge_and_positive_with_one():
    assert kelly_fraction(0.95, 0.0, 0.028) == 0.0
    f = kelly_fraction(0.95, 0.05, 0.0)
    assert 0 < f < 1
    # Standard Kelly for p=0.55, b=0.95.
    p = 0.55
    assert kelly_fraction(0.95, 0.05, 0.0) == pytest.approx(
        (0.95 * p - (1 - p)) / 0.95, abs=1e-12)


@pytest.mark.parametrize("tie", [-0.01, 1.5, 2.0])
def test_invalid_tie_rate_rejected(tie):
    with pytest.raises(ValueError):
        required_edge(0.9, tie)


def test_a_fully_frozen_feed_is_degenerate_rather_than_an_error():
    """p_tie == 1.0 is observable (a market open but not quoting), so it is
    handled as a data fault rather than raising out of the analysis."""
    assert required_edge(0.9, 1.0, "strict") == math.inf
    assert required_edge(0.9, 1.0, "equals") == -math.inf


def test_unknown_variant_rejected():
    with pytest.raises(ValueError):
        required_edge(0.9, 0.01, "sideways")


def test_wilson_interval_brackets_the_estimate_and_stays_in_range():
    lo, hi = wilson_interval(28, 1000)
    assert 0 < lo < 0.028 < hi < 1
    # Unlike Wald, it never goes negative at zero successes.
    lo0, hi0 = wilson_interval(0, 500)
    assert lo0 == pytest.approx(0.0, abs=1e-12) and 0 < hi0 < 0.02
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_narrows_with_sample_size():
    small = wilson_interval(30, 1000)
    large = wilson_interval(300, 10000)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_bootstrap_median_interval_brackets_the_median():
    values = [0.90 + 0.01 * (i % 7) for i in range(400)]
    lo, hi = bootstrap_median_interval(values)
    assert lo <= sorted(values)[len(values) // 2] <= hi
    assert all(math.isnan(v) for v in bootstrap_median_interval([]))
    assert bootstrap_median_interval([0.9]) == (0.9, 0.9)


def test_evaluate_bundles_consistent_values():
    econ = evaluate(0.92, 0.02, "strict")
    assert econ.breakeven_probability == pytest.approx(1 / 1.92)
    assert econ.house_margin == pytest.approx(1 / 1.92 - 0.5)
    assert econ.ev_at(econ.required_edge) == pytest.approx(0.0, abs=1e-12)
    assert econ.ev_at_zero_edge < 0
