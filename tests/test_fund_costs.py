"""The cost model decides which fund wins, so it must be exactly right."""

import pytest

from fund_census.costs import (DEFAULT_WITHHOLDING, FundCosts, PlatformCosts,
                               WithholdingModel, annual_drag, entry_cost_rate,
                               net_annual_return, total_annual_drag)

PLATFORM = PlatformCosts(commission_per_trade=1.0, fx_spread=0.002)


def test_withholding_layers_compound_rather_than_add():
    """A 15% fund-level tax followed by a 30% investor-level tax does not
    leave 55% of the dividend; the second applies to what survives the first."""
    model = WithholdingModel(fund_level_rate=0.15, investor_level_rate=0.30)
    assert model.total_leakage == pytest.approx(1 - 0.85 * 0.70)
    assert model.total_leakage == pytest.approx(0.405)
    assert model.total_leakage != pytest.approx(0.45)   # not the naive sum


def test_no_withholding_means_no_leakage():
    assert WithholdingModel(0.0, 0.0).total_leakage == pytest.approx(0.0)


def test_full_withholding_leaves_nothing():
    assert WithholdingModel(1.0, 0.0).total_leakage == pytest.approx(1.0)


def test_the_cheaper_fund_by_fee_can_be_the_dearer_fund_in_total():
    """The finding the tool exists to surface: for an investor with no US tax
    treaty, a US-domiciled fund with a third of the fee costs more to hold,
    because its distributions are taxed at the full statutory rate."""
    irish = FundCosts("Irish", ter=0.0022, domicile="IE")
    american = FundCosts("US", ter=0.0007, domicile="US")

    assert american.ter < irish.ter                        # cheaper on paper
    irish_total = total_annual_drag(irish, PLATFORM, 0.018)
    us_total = total_annual_drag(american, PLATFORM, 0.018)
    assert us_total > irish_total                          # dearer in fact

    # And the term that flips it is the one nobody compares.
    us_parts = annual_drag(american, PLATFORM, 0.018)
    assert us_parts["dividend_withholding"] > us_parts["ter"] * 5


def test_withholding_scales_with_dividend_yield():
    fund = FundCosts("x", ter=0.002, domicile="US")
    low = annual_drag(fund, PLATFORM, 0.01)["dividend_withholding"]
    high = annual_drag(fund, PLATFORM, 0.03)["dividend_withholding"]
    assert high == pytest.approx(low * 3)
    assert annual_drag(fund, PLATFORM, 0.0)["dividend_withholding"] == 0.0


def test_drag_components_sum_to_the_total():
    fund = FundCosts("x", ter=0.002, domicile="IE", tracking_difference=0.0005)
    platform = PlatformCosts(platform_fee=0.0025)
    parts = annual_drag(fund, platform, 0.018)
    assert sum(parts.values()) == pytest.approx(
        total_annual_drag(fund, platform, 0.018))
    assert set(parts) == {"ter", "tracking_difference",
                          "dividend_withholding", "platform_fee"}


def test_a_verified_withholding_rate_overrides_the_domicile_default():
    verified = WithholdingModel(0.15, 0.0, basis="confirmed with adviser")
    fund = FundCosts("x", ter=0.002, domicile="US", withholding=verified)
    assert fund.withholding_model() is verified
    assert "adviser" in fund.withholding_model().basis


def test_an_unknown_domicile_falls_back_without_crashing():
    fund = FundCosts("x", ter=0.002, domicile="ZZ")
    assert 0 < fund.withholding_model().total_leakage < 1
    assert "assumed" in fund.withholding_model().basis


def test_every_default_withholding_model_carries_its_basis():
    """These rates decide the ranking, so where they came from is not optional."""
    for code, model in DEFAULT_WITHHOLDING.items():
        assert model.basis and "VERIFY" in model.basis, code
        assert 0 <= model.total_leakage < 1


def test_entry_cost_includes_half_the_spread_not_all_of_it():
    fund = FundCosts("x", ter=0.0, spread=0.001)
    plain = PlatformCosts()
    assert entry_cost_rate(fund, plain, 1000) == pytest.approx(0.0005)


def test_a_flat_commission_is_brutal_on_small_purchases():
    """The argument against contributing tiny amounts very frequently."""
    fund = FundCosts("x", ter=0.0, spread=0.0)
    platform = PlatformCosts(commission_per_trade=1.0)
    assert entry_cost_rate(fund, platform, 50) == pytest.approx(0.02)    # 2%
    assert entry_cost_rate(fund, platform, 5000) == pytest.approx(0.0002)


def test_entry_cost_rejects_a_zero_purchase():
    with pytest.raises(ValueError):
        entry_cost_rate(FundCosts("x", ter=0.0), PlatformCosts(), 0)


def test_costs_are_charged_on_the_balance_not_deducted_from_the_return():
    """A 1% fee does not simply turn 8% into 7%: it takes 1% of everything."""
    naive = 0.08 - 0.01
    actual = net_annual_return(0.08, 0.01)
    assert actual < naive
    assert actual == pytest.approx(1.08 * 0.99 - 1.0)


def test_zero_drag_returns_the_gross_return():
    assert net_annual_return(0.07, 0.0) == pytest.approx(0.07)
