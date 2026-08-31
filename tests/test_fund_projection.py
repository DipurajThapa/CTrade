"""What the costs do to a balance over time."""

import pytest

from fund_census.costs import FundCosts, PlatformCosts
from fund_census.projection import (Plan, break_even_years,
                                    contribution_sensitivity, project)

PLATFORM = PlatformCosts(commission_per_trade=1.0, fx_spread=0.002)
CHEAP = FundCosts("cheap", ter=0.0007, domicile="IE", spread=0.0002)
DEAR = FundCosts("dear", ter=0.0100, domicile="US", spread=0.0020)


def test_a_lump_sum_compounds_at_the_net_rate():
    plan = Plan(initial_amount=1000, monthly_contribution=0, horizon_years=10,
                gross_return=0.07)
    fund = FundCosts("x", ter=0.0, domicile="IE", spread=0.0)
    p = project(fund, PlatformCosts(), plan)
    assert p.terminal_wealth == pytest.approx(
        1000 * (1 + p.net_annual_return) ** 10, rel=1e-6)


def test_a_frictionless_fund_costs_nothing():
    plan = Plan(initial_amount=1000, monthly_contribution=0, horizon_years=10)
    fund = FundCosts("free", ter=0.0, domicile="IE", spread=0.0)
    zero = PlatformCosts()
    p = project(fund, zero, plan)
    # No fee, no spread, but withholding still applies -- it is a tax, not a
    # charge, and no fund can waive it.
    assert p.entry_cost_rate == 0.0
    assert p.annual_drag > 0
    assert p.annual_drag == pytest.approx(
        plan.dividend_yield * fund.withholding_model().total_leakage)


def test_cost_is_measured_at_the_horizon_not_as_a_running_total():
    """A fee paid in year one also removes every year of growth it would
    have earned, so the honest figure is the shortfall at the end."""
    plan = Plan(initial_amount=10_000, monthly_contribution=0,
                horizon_years=30, gross_return=0.07)
    p = project(DEAR, PLATFORM, plan)
    naive = 10_000 * p.annual_drag * 30            # fees, ignoring compounding
    assert p.total_cost > naive * 1.5
    assert p.cost_share == pytest.approx(p.total_cost / p.frictionless_terminal)


def test_the_cheaper_fund_wins_and_the_gap_grows_with_horizon():
    short = Plan(initial_amount=10_000, monthly_contribution=0, horizon_years=5)
    long = Plan(initial_amount=10_000, monthly_contribution=0, horizon_years=30)
    gap_short = (project(CHEAP, PLATFORM, short).terminal_wealth
                 - project(DEAR, PLATFORM, short).terminal_wealth)
    gap_long = (project(CHEAP, PLATFORM, long).terminal_wealth
                - project(DEAR, PLATFORM, long).terminal_wealth)
    assert 0 < gap_short < gap_long


def test_contributions_are_counted_and_growth_is_the_remainder():
    plan = Plan(initial_amount=1000, monthly_contribution=500,
                horizon_years=10, gross_return=0.07)
    p = project(CHEAP, PLATFORM, plan)
    assert p.total_contributed == pytest.approx(1000 + 500 * 120)
    assert p.gain == pytest.approx(p.terminal_wealth - p.total_contributed)
    assert p.gain > 0


def test_contributing_more_beats_a_higher_return_at_a_small_balance():
    """The finding that matters most to someone starting with very little:
    the lever they control is worth more than the one they can only hope for."""
    plan = Plan(initial_amount=100, monthly_contribution=500,
                horizon_years=20, gross_return=0.07)
    s = contribution_sensitivity(CHEAP, PLATFORM, plan)
    doubled_contribution = s["contribution x2"]
    plus_four_points = s["return +4%"]
    assert doubled_contribution > plus_four_points


def test_a_zero_horizon_returns_the_opening_balance_less_entry_cost():
    plan = Plan(initial_amount=1000, monthly_contribution=0, horizon_years=0)
    p = project(CHEAP, PLATFORM, plan)
    assert p.terminal_wealth == pytest.approx(
        1000 * (1 - p.entry_cost_rate))
    assert p.terminal_wealth < 1000


def test_break_even_reports_none_when_a_switch_never_repays():
    plan = Plan(initial_amount=1000, monthly_contribution=0, horizon_years=5)
    cheap, dear = project(CHEAP, PLATFORM, plan), project(DEAR, PLATFORM, plan)
    assert break_even_years(cheap, dear, switching_cost=0.0, plan=plan) == 0.0
    assert break_even_years(cheap, dear, switching_cost=10_000, plan=plan) is None
    years = break_even_years(cheap, dear, switching_cost=20, plan=plan)
    assert years is not None and 0 < years < 5


def test_switching_to_a_dearer_fund_never_breaks_even():
    plan = Plan(initial_amount=1000, monthly_contribution=0, horizon_years=20)
    cheap, dear = project(CHEAP, PLATFORM, plan), project(DEAR, PLATFORM, plan)
    assert break_even_years(dear, cheap, switching_cost=50, plan=plan) is None
