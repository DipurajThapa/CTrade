"""What a fund actually costs, as opposed to what its factsheet advertises.

An index fund investor is not trying to beat a market. They are trying to
receive the market's return with as little of it removed as possible. So the
measurement that decides outcomes is total drag: everything between the index
return and the money that reaches the account.

The headline fee is the smallest interesting part of that.

The term that dominates comparisons, and appears in no comparison table, is
dividend withholding tax. A fund's domicile determines what tax is levied on
the dividends it receives and on what it pays out, and for an investor in a
country with no relevant tax treaty the difference between two otherwise
identical global funds can exceed both of their management fees combined.

This is the same shape of finding as the settlement-tie rate in the binary
census: a cost of the same order as the one everybody looks at, invisible
unless you measure it, and decisive over a long horizon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_WITHHOLDING",
    "WithholdingModel",
    "FundCosts",
    "PlatformCosts",
    "annual_drag",
    "total_annual_drag",
    "entry_cost_rate",
    "net_annual_return",
]


@dataclass(frozen=True)
class WithholdingModel:
    """Effective tax leakage on the dividends a fund receives and distributes.

    Two layers matter, and only the second is visible to most investors:

    **Fund level.** A fund domiciled in Ireland holding US equities pays US
    withholding tax on the dividends it receives, reduced by the US-Ireland
    treaty. A fund domiciled in the US pays none on its own domestic holdings.

    **Investor level.** What the fund's own jurisdiction withholds when it
    pays out. Ireland withholds nothing from non-residents. The US withholds
    from foreign investors at a rate set by treaty -- or at the full statutory
    rate where no treaty exists.

    For an investor whose country has no US income tax treaty, this reverses
    the naive ranking: the US-domiciled fund usually has the lower headline
    fee and the higher total cost, because its distributions are taxed at the
    full rate on the way out.

    THESE RATES MUST BE VERIFIED. Treaty status and statutory rates change,
    and they depend on the investor's tax residency, not their location. The
    defaults below are conservative starting points for modelling, not tax
    advice, and a wrong assumption here is worth more than every other input
    in this module combined.
    """

    #: Tax the fund itself suffers on dividends from its holdings.
    fund_level_rate: float
    #: Tax withheld when the fund distributes to this investor.
    investor_level_rate: float
    #: Free-text record of where these numbers came from.
    basis: str = "unverified default"

    @property
    def total_leakage(self) -> float:
        """Combined fraction of gross dividends lost to withholding.

        The layers compound rather than add: the investor-level rate applies
        to what survives the fund level.
        """
        surviving = (1.0 - self.fund_level_rate) * (1.0 - self.investor_level_rate)
        return 1.0 - surviving


#: Starting points for an investor with NO US income tax treaty, holding a
#: global equity index fund with roughly a 60% US weighting. Verify before use.
DEFAULT_WITHHOLDING: dict[str, WithholdingModel] = {
    # Irish UCITS: 15% US tax at fund level under the US-Ireland treaty,
    # nothing withheld by Ireland on the way out to a non-resident.
    "IE": WithholdingModel(
        fund_level_rate=0.15 * 0.60 + 0.10 * 0.40,
        investor_level_rate=0.0,
        basis="US-Ireland treaty 15% on US holdings; ~10% blended elsewhere; "
              "Ireland withholds nothing from non-residents. VERIFY."),
    # US-domiciled: no fund-level tax on its own US holdings, but the full
    # statutory 30% is withheld from distributions where no treaty applies.
    "US": WithholdingModel(
        fund_level_rate=0.10 * 0.40,
        investor_level_rate=0.30,
        basis="No US treaty assumed: statutory 30% on distributions to the "
              "investor; ~10% blended on non-US holdings at fund level. VERIFY."),
    # Luxembourg UCITS, broadly similar to Ireland but with a slightly worse
    # US treaty rate on some share classes.
    "LU": WithholdingModel(
        fund_level_rate=0.15 * 0.60 + 0.10 * 0.40,
        investor_level_rate=0.0,
        basis="Comparable to Ireland for most UCITS structures. VERIFY."),
}


@dataclass
class FundCosts:
    """One candidate fund and the costs of holding it."""

    name: str
    #: Total expense ratio, annual, as a fraction. From the factsheet.
    ter: float
    #: Domicile code, which selects the withholding model.
    domicile: str = "IE"
    #: Underperformance versus the index BEYOND the TER, annual. Published
    #: tracking difference minus TER. Can be negative when securities lending
    #: income offsets fees.
    tracking_difference: float = 0.0
    #: Full quoted bid-ask spread as a fraction of price.
    spread: float = 0.0005
    #: Overrides the domicile default when a verified rate is available.
    withholding: WithholdingModel | None = None

    def withholding_model(self) -> WithholdingModel:
        if self.withholding is not None:
            return self.withholding
        return DEFAULT_WITHHOLDING.get(
            self.domicile.upper(),
            WithholdingModel(0.15, 0.0, "unknown domicile; Ireland assumed"))


@dataclass
class PlatformCosts:
    """What the broker charges, independent of which fund is bought."""

    #: Flat commission per purchase, in account currency.
    commission_per_trade: float = 0.0
    #: Commission as a fraction of each purchase.
    commission_rate: float = 0.0
    #: Cost of converting the account currency into the fund's, as a fraction.
    #: Retail bank rates are frequently ten times a good broker's.
    fx_spread: float = 0.0
    #: Annual custody or platform fee as a fraction of assets.
    platform_fee: float = 0.0


def annual_drag(fund: FundCosts, platform: PlatformCosts,
                dividend_yield: float) -> dict[str, float]:
    """Recurring annual cost, decomposed so the dominant term is visible.

    Returned as a breakdown rather than a single number precisely because the
    ranking usually turns on the component nobody looks at.
    """
    leakage = fund.withholding_model().total_leakage
    return {
        "ter": fund.ter,
        "tracking_difference": fund.tracking_difference,
        "dividend_withholding": dividend_yield * leakage,
        "platform_fee": platform.platform_fee,
    }


def total_annual_drag(fund: FundCosts, platform: PlatformCosts,
                      dividend_yield: float) -> float:
    return sum(annual_drag(fund, platform, dividend_yield).values())


def entry_cost_rate(fund: FundCosts, platform: PlatformCosts,
                    purchase_amount: float) -> float:
    """One-off cost of putting money in, as a fraction of the amount.

    Half the quoted spread is paid crossing to the offer. A flat commission is
    included as a rate here because that is what makes it comparable: the same
    fee is trivial on a large purchase and severe on a small one, which is the
    argument against contributing tiny amounts very frequently.
    """
    if purchase_amount <= 0:
        raise ValueError("purchase_amount must be positive")
    flat = platform.commission_per_trade / purchase_amount
    return fund.spread / 2.0 + platform.fx_spread + platform.commission_rate + flat


def net_annual_return(gross_return: float, drag: float) -> float:
    """Return after costs.

    Costs are levied on the whole balance, so they compound against the
    portfolio rather than subtracting from the return: a fund charging 1%
    does not simply turn 8% into 7%, it takes 1% of everything every year for
    as long as the money is held.
    """
    return (1.0 + gross_return) * (1.0 - drag) - 1.0
